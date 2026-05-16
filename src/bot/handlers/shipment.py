"""Заявки на отгрузку (новый основной flow).

Pipeline:
  1. /ship                — список открытых заявок или кнопка «новая»
  2. кидаешь xlsx-кластер — парсим, спрашиваем «в новую или в существующую?»
  3. (этап 2 — отдельно)  — wizard дат/кросс-док
  4. (этап 3 — отдельно)  — слот-хантер
  5. (этап 4 — отдельно)  — генерация ТЗ Отгрузка

Сейчас реализован шаг 1-2.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from aiogram import Router, F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from src.bot.helpers import safe_edit_or_answer, send_long
from src.config import STORAGE_DIR
from src.db.session import db_session
from src.parsers.ship_request import parse_ship_file, ShipFileParsed
from src.services.shipment_service import (
    create_shipment_request, attach_ship_file,
    list_shipment_requests, get_shipment_request,
    shipment_summary,
)
from src.services.user_service import (
    current_user_id_from,
    get_ozon_client_for,
    get_wb_api_key,
)

_NO_OZON_KEYS_MSG_SHIP = "⚠ Ozon-ключи не настроены. Открой /start → «Добавить Ozon»."

router = Router()
logger = logging.getLogger("bot.shipment")


# Перевод служебных state-значений в человекочитаемые подписи (для UI).
# DB-значения сохраняются как есть — переводим только при отображении.
_STATE_LABELS = {
    "draft": "📥 Подбор файлов",
    "planning": "📅 Планирование",
    "slot_searching": "🔍 Поиск слотов",
    "slots_booked": "✅ Забронировано",
    "supplies_created": "✅ Забронировано",
    "tz_sent": "📤 ТЗ отправлено",
    "picked": "📦 Собрано",
    "shipped": "🚛 Отгружено",
    "accepted": "🏁 Принято",
}


def _state_label(state: str) -> str:
    return _STATE_LABELS.get(state, state)


def _compute_display_label(req) -> str:
    """Отображаемый статус заявки. Считается по реальному состоянию items,
    а не по устаревшему полю req.state. Логика:

    - Нет items → «📥 Подбор файлов»
    - 0 направлений забронировано → «📝 Черновик»
    - Не все направления забронированы → «📦 Частично сформировано»
    - Все забронированы + один Ozon-статус у всех → берём его (📝 Заполнение
      данных / 📦 Готово к отгрузке / 🚛 В пути / 📥 Приёмка / ✅ Завершено …)
    - Все забронированы, но статусы разные или не подтянулись → «✅ Забронировано»
    """
    if not req.items:
        return _STATE_LABELS["draft"]
    clusters = {(it.marketplace, it.cluster) for it in req.items}
    booked_clusters = {
        (it.marketplace, it.cluster) for it in req.items if it.booked_supply_id
    }
    if not booked_clusters:
        return "📝 Черновик"
    if booked_clusters != clusters:
        return "📦 Частично сформировано"
    # Все забронированы — смотрим Ozon-статусы.
    statuses = {
        (it.ozon_supply_status or "").upper()
        for it in req.items
        if it.marketplace == "ozon" and it.booked_supply_id
    }
    statuses.discard("")
    if len(statuses) == 1:
        from src.services.ozon_supply_status_service import status_info
        si = status_info(next(iter(statuses)))
        return f"{si.emoji} {si.label}"
    if len(statuses) > 1:
        return "🚛 В работе"
    return "✅ Забронировано"


def _ru_plural(n: int, one: str, few: str, many: str) -> str:
    """Русский plural: 1, 21 → one; 2-4, 22-24 → few; 5-20, 0, 11-14 → many."""
    n = abs(n)
    n100 = n % 100
    n10 = n % 10
    if 11 <= n100 <= 14:
        return many
    if n10 == 1:
        return one
    if 2 <= n10 <= 4:
        return few
    return many


class ShipPick(StatesGroup):
    pick_request = State()


class ShipNewType(StatesGroup):
    """Выбор типа Ozon-поставки (direct/cross) до создания заявки."""
    pick_otype = State()


class ShipPlan(StatesGroup):
    dates = State()
    hours = State()                  # выбор часов суток после дат (time-picker)
    crossdock_mode = State()         # один склад для всех или индивидуально
    crossdock_each_pick = State()    # выбор направления (если индивидуально)
    crossdock_each_set = State()     # выбор склада для конкретного направления
    confirm = State()


# Кросс-док только для Ozon (через WB API поставку всё равно не создать)
CROSSDOCK_OPTIONS = [
    "Прямая поставка (без кросс-дока)",
    "Через хаб → Внуково (Ozon)",
    "Через хаб → Хоругвино (Ozon)",
    "Любой подходящий",
]

# Кэш результатов ханта: (rid, mp, wid) → {name, dlv_coef, coef, dates: [iso]}.
# Используется для двухступенчатого выбора: склад → дата. Volatile (в памяти).
_HUNT_CACHE: Dict[tuple, dict] = {}


# ── /ship ───────────────────────────────────────────────────────────────────

@router.message(Command("ship"))
async def cmd_ship(msg: Message) -> None:
    tg_id = msg.from_user.id if msg.from_user else 0
    await _render_ship_list(msg, tg_id=tg_id, edit=False)


@router.callback_query(F.data.startswith("ships_ext:"))
async def cb_ships_toggle_external(cb: CallbackQuery) -> None:
    """Toggle: показать/скрыть Ozon-поставки созданные не в боте."""
    await cb.answer()
    if not cb.message:
        return
    show = cb.data.split(":", 1)[1] == "on"
    tg_id = cb.from_user.id if cb.from_user else 0
    await _render_ship_list(cb.message, tg_id=tg_id, edit=True, show_external=show)


async def _collect_external_supplies(
    tg_id: int, own_ids: set,
) -> tuple[list, list[str]]:
    """Тянет supply_orders из Ozon ЛК которые НЕ создавались ботом.
    Возвращает (kb_rows, text_lines). Каждая строка — кнопка-ссылка
    на конкретную поставку в Ozon ЛК."""
    from src.db.session import db_session
    from src.services.user_service import get_ozon_client_for
    from src.integrations.ozon_api import OzonAPIError
    rows: list = []
    lines: list[str] = []
    with db_session() as s:
        oz = get_ozon_client_for(s, tg_id)
    if oz is None:
        return rows, ["", "<i>🌐 Ozon-ключи не подключены — список из ЛК недоступен.</i>"]
    try:
        # Все актуальные поставки (исключая cancelled/completed — это шумно).
        ext_ids = await oz.supply_order_list(
            states=[
                "DATA_FILLING", "READY_TO_SUPPLY", "ACCEPTED_AT_SUPPLY_WAREHOUSE",
                "IN_TRANSIT", "ACCEPTANCE_AT_STORAGE_WAREHOUSE",
                "REPORTS_CONFIRMATION_AWAITING", "REPORT_REJECTED",
            ],
            max_total=50,
        )
    except OzonAPIError as e:
        return rows, ["", f"<i>🌐 Ozon API: <code>{str(e)[:120]}</code></i>"]
    # Оставляем только те что НЕ из бота.
    foreign = [oid for oid in ext_ids if oid not in own_ids]
    if not foreign:
        return rows, ["", "🌐 <i>В Ozon ЛК нет других активных поставок.</i>"]
    try:
        orders = await oz.supply_order_get(foreign[:30])
    except OzonAPIError as e:
        return rows, ["", f"<i>🌐 Ozon supply_order_get: <code>{str(e)[:120]}</code></i>"]
    state_emoji = {
        "DATA_FILLING": "📝", "READY_TO_SUPPLY": "📦",
        "ACCEPTED_AT_SUPPLY_WAREHOUSE": "🏭", "IN_TRANSIT": "🚚",
        "ACCEPTANCE_AT_STORAGE_WAREHOUSE": "🏬",
        "REPORTS_CONFIRMATION_AWAITING": "📋", "REPORT_REJECTED": "🔴",
    }
    lines.append("")
    lines.append(f"🌐 <b>В Ozon ЛК</b> ({len(orders)} поставок не из бота)")
    for o in orders:
        order_id = int(o.get("order_id") or 0)
        order_number = str(o.get("order_number") or "?")
        state = str(o.get("state") or "")
        drop = o.get("drop_off_warehouse") or {}
        wh_name = (drop.get("name") or "")[:25]
        emoji = state_emoji.get(state, "🔵")
        # Кнопка-ссылка прямо на поставку в Ozon ЛК.
        url = f"https://seller.ozon.ru/app/supply/orders/{order_id}"
        rows.append([InlineKeyboardButton(
            text=f"{emoji} #{order_number} · {wh_name}"[:55],
            url=url,
        )])
    return rows, lines


async def _render_ship_list(
    target: Message,
    *,
    tg_id: int,
    edit: bool = False,
    show_external: bool = False,
) -> None:
    """Список поставок с инлайн-кнопками. Группировка: WB / Ozon / Смешанные.
    edit=True пытается отредактировать сообщение target вместо answer.
    show_external=True — добавляем секцию «Поставки из Ozon ЛК (не из бота)».
    `tg_id` — обязательный, чтобы list не утекал между кабинетами.
    """
    wb_rows: List[InlineKeyboardButton] = []
    oz_rows: List[InlineKeyboardButton] = []
    mix_rows: List[InlineKeyboardButton] = []
    total = 0
    own_order_ids: set = set()  # supply_id'ы созданные ботом (для исключения из external)
    with db_session() as session:
        reqs = list_shipment_requests(session, user_id=tg_id, limit=30)
        for r in reqs:
            for it in r.items:
                if it.booked_supply_id:
                    try:
                        own_order_ids.add(int(it.booked_supply_id))
                    except (ValueError, TypeError):
                        pass
            # Прячем только окончательно закрытые/отменённые. supplies_created и
            # дальнейшие статусы (tz_sent / picked / shipped / accepted) должны
            # быть видны — там как раз отслеживаются статусы Ozon-поставок.
            if r.state in {"closed", "cancelled"}:
                continue
            total += 1
            mps = {it.marketplace for it in r.items}
            cluster_set = {(it.marketplace, it.cluster) for it in r.items}
            n_clusters = len(cluster_set)
            date_s = r.created_at.strftime("%d.%m")
            if mps == {"wb"}:
                emoji = "🟣"; bucket = wb_rows
            elif mps == {"ozon"}:
                emoji = "🔵"; bucket = oz_rows
            else:
                emoji = "🟡"; bucket = mix_rows
            if n_clusters == 1:
                # Одно направление — показываем его имя вместо счётчика.
                cl_name = next(iter(cluster_set))[1]
                cluster_part = cl_name
            else:
                cl_word = _ru_plural(n_clusters, "направление", "направления", "направлений")
                cluster_part = f"{n_clusters} {cl_word}"
            label = f"{emoji} #{r.id} [{_compute_display_label(r)}] · {cluster_part} · {date_s}"
            bucket.append(InlineKeyboardButton(text=label[:55], callback_data=f"ship_open:{r.id}"))

    new_btn = InlineKeyboardButton(text="➕ Новая поставка (шаблон xlsx)",
                                   callback_data="ship_new_template")

    # Toggle-кнопка для внешних поставок (созданных не в боте, в Ozon ЛК).
    ext_toggle = InlineKeyboardButton(
        text=("🌐 Скрыть поставки из Ozon ЛК" if show_external
              else "🌐 Показать поставки из Ozon ЛК"),
        callback_data=f"ships_ext:{'off' if show_external else 'on'}",
    )

    if not total:
        lines = ["🚚 <b>Поставки</b>\n\nИз бота — пусто.\n\n📎 Кинь .xlsx-выгрузку или скачай шаблон ниже."]
        rows = [[new_btn], [ext_toggle]]
    else:
        lines = ["🚚 <b>Поставки</b>\n"]
        rows = [[new_btn]]
        if wb_rows:
            lines.append(f"🟣 <b>WB</b> ({len(wb_rows)})")
            rows.extend([[b] for b in wb_rows])
        if oz_rows:
            lines.append(f"🔵 <b>Ozon</b> ({len(oz_rows)})")
            rows.extend([[b] for b in oz_rows])
        if mix_rows:
            lines.append(f"🟡 <b>Смешанные</b> ({len(mix_rows)})")
            rows.extend([[b] for b in mix_rows])
        lines.append("\n📎 Кинь .xlsx — добавить ещё")
        rows.append([ext_toggle])
        rows.append([InlineKeyboardButton(text="🗑 Удалить поставки",
                                          callback_data="ships_delete_picker")])

    # Внешние поставки из Ozon ЛК (не наши). Только если юзер попросил —
    # это +2-3 секунды на API-вызовы.
    if show_external:
        external_rows, ext_text_lines = await _collect_external_supplies(
            target.chat.id, own_order_ids,
        )
        lines.extend(ext_text_lines)
        rows.extend(external_rows)

    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:home")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    text = "\n".join(lines)

    if edit:
        try:
            await target.edit_text(text, reply_markup=kb)
            return
        except Exception:
            pass
    await target.answer(text, reply_markup=kb)


@router.message(Command("ship_show"))
async def cmd_ship_show(msg: Message, command: CommandObject) -> None:
    try:
        rid = int((command.args or "").strip())
    except ValueError:
        await msg.answer("Использование: <code>/ship_show ID</code>")
        return
    tg_id = msg.from_user.id if msg.from_user else 0
    with db_session() as session:
        req = get_shipment_request(session, rid, user_id=tg_id)
        if not req:
            await msg.answer(f"Поставка #{rid} не найдена.")
            return
        text, kb = _render_request_card(req)
    await msg.answer(text, reply_markup=kb)


def _render_request_card(req) -> tuple:
    """Внутри сессии собрать текст карточки + клавиатуру."""
    # Идемпотентный пересчёт state — закрывает баг когда заявка осталась в planning
    # хотя все items забронированы (refresh_request_state_after_booking не сработал).
    from src.services.shipment_service import refresh_request_state_after_booking
    refresh_request_state_after_booking(req)
    summary = shipment_summary(req)
    files = req.source_files_json or []
    crossdock = req.crossdock_warehouses_json or {}
    has_ozon = any(it.marketplace == "ozon" for it in req.items)
    has_wb = any(it.marketplace == "wb" for it in req.items)

    text = (
        f"📦 <b>Поставка #{req.id}</b> [{_compute_display_label(req)}]\n"
        f"Создана: {req.created_at:%Y-%m-%d %H:%M}\n"
        f"Файлов: {len(files)}\n\n"
        f"<b>Распределение:</b>\n{summary}"
    )

    if req.target_date_from:
        # Реально выбранные даты (target_dates_json) приоритетнее диапазона —
        # юзер мог выбрать [20, 23] а в from..to сидит «20—23» что обманывает.
        from src.bot.helpers import format_picked_dates, format_picked_hours
        date_s = format_picked_dates(
            req.target_dates_json,
            fallback_from=req.target_date_from,
            fallback_to=req.target_date_to,
        )
        text += f"\n\n<b>Целевые даты:</b> {date_s}"
        hours_s = format_picked_hours(req.target_hours_json)
        text += f"\n<b>Часы:</b> {hours_s}"

    if crossdock:
        text += "\n\n<b>Кросс-док:</b>"
        for k, v in crossdock.items():
            text += f"\n  {k}: {v}"

    if has_ozon and req.ozon_supply_type:
        otype_label = "🚚 Прямая" if req.ozon_supply_type == "direct" else "🔀 Кросс-докинг"
        text += f"\n\n<b>Тип Ozon-поставки:</b> {otype_label}"
    if has_ozon and req.cargo_format:
        fmt_label = "📦 Коробами" if req.cargo_format == "BOX" else "🏗 Паллетами"
        text += f"\n<b>Формат:</b> {fmt_label}"

    # Статусы Ozon supply-orders — для каждого направления с booked_supply_id.
    booked_ozon = [it for it in req.items if it.marketplace == "ozon" and it.booked_supply_id]
    if booked_ozon:
        from src.services.ozon_supply_status_service import status_info
        # Группируем по cluster+booked_supply_id (одно направление = много items).
        by_supply: Dict[str, ShipmentItem] = {}
        for it in booked_ozon:
            by_supply.setdefault(it.booked_supply_id, it)
        text += "\n\n<b>Ozon поставки:</b>"
        for bsid, it in by_supply.items():
            si = status_info(it.ozon_supply_status, it.ozon_order_number)
            num = f" · ЛК <code>{si.order_number}</code>" if si.order_number else ""
            status_part = f"{si.emoji} {si.label}" if si.state else "⏳ статус не запрашивался"
            text += f"\n  {it.cluster}: {status_part}{num}"

    rows = []
    # Есть ли вообще что бронировать ещё (хотя бы один item без booked_supply_id).
    has_unbooked_ozon = any(
        it.marketplace == "ozon" and not it.booked_supply_id for it in req.items
    )
    has_unbooked_wb = any(
        it.marketplace == "wb" and not it.booked_supply_id for it in req.items
    )
    # Этап 1: даты ещё не выбраны
    if req.state == "draft":
        rows.append([InlineKeyboardButton(text="🛠 Спланировать даты",
                                         callback_data=f"ship_plan:{req.id}")])
    else:
        # Этап 2: даты есть → разные кнопки в зависимости от MP
        if has_wb and has_unbooked_wb:
            rows.append([InlineKeyboardButton(text="🔍 Подобрать склад WB",
                                             callback_data=f"ship_hunt:{req.id}")])
        if has_ozon and has_unbooked_ozon:
            # Тип Ozon-поставки фиксируется при создании. NULL = legacy-заявка
            # до миграции — даём юзеру выбрать тип однократно прямо здесь.
            # cargo_format (BOX/PALLET) — спрашиваем тоже, перед запуском wizard'а.
            if req.ozon_supply_type and not req.cargo_format:
                # Тип уже задан, осталось спросить формат
                rows.append([InlineKeyboardButton(
                    text="🚀 Создать поставку Ozon (выбрать формат)",
                    callback_data=f"ship_pick_fmt:{req.id}",
                )])
            elif req.ozon_supply_type == "direct":
                rows.append([InlineKeyboardButton(
                    text="🚀 Создать поставку Ozon → Прямая",
                    callback_data=f"ozon_book_card:{req.id}:direct",
                )])
            elif req.ozon_supply_type == "cross":
                rows.append([InlineKeyboardButton(
                    text="🚛 Создать поставку Ozon → Кросс-докинг",
                    callback_data=f"ozon_book_card:{req.id}:cross",
                )])
            else:
                rows.append([InlineKeyboardButton(
                    text="🚚 Ozon — Прямая",
                    callback_data=f"ship_set_otype:{req.id}:d",
                )])
                rows.append([InlineKeyboardButton(
                    text="🔀 Ozon — Кросс-докинг",
                    callback_data=f"ship_set_otype:{req.id}:c",
                )])
        # «Изменить даты» имеет смысл пока не всё забронировано.
        if has_unbooked_ozon or has_unbooked_wb:
            rows.append([InlineKeyboardButton(text="🛠 Изменить даты",
                                             callback_data=f"ship_plan:{req.id}")])
    if has_wb:
        rows.append([InlineKeyboardButton(text="🌐 WB ЛК → Поставки",
                                         url="https://seller.wildberries.ru/supplies-management/all-supplies")])
    if has_ozon:
        rows.append([InlineKeyboardButton(text="🌐 Ozon ЛК → Поставки",
                                         url="https://seller.ozon.ru/app/supply-orders")])
    if booked_ozon:
        rows.append([InlineKeyboardButton(
            text="🔄 Обновить статусы Ozon",
            callback_data=f"ship_refresh_oz:{req.id}",
        )])
        rows.append([InlineKeyboardButton(
            text="🗑 Отменить все Ozon-поставки заявки",
            callback_data=f"ship_cancel_oz:{req.id}",
        )])
    rows.append([
        InlineKeyboardButton(text="🛒 Состав по кластерам", callback_data=f"ship_items:{req.id}"),
    ])
    rows.append([
        InlineKeyboardButton(text="📤 ТЗ xlsx", callback_data=f"ship_tz:{req.id}"),
    ])
    rows.append([
        InlineKeyboardButton(text="◀ К списку", callback_data="menu:ships"),
    ])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    return text, kb


# ── приём xlsx-файла отгрузки ────────────────────────────────────────────────

def _looks_like_ship_file(fname: str) -> bool:
    """Эвристика: имя совпадает с '<кластер>_YYYY-MM-DD_*.xlsx'.
    Telegram при отправке заменяет '-' на '_', поэтому ловим оба варианта."""
    import re
    return bool(re.search(r"_\d{4}[-_]\d{2}[-_]\d{2}", fname or ""))


def looks_like_ship_file(fname: str) -> bool:
    """Публичный helper для upload.py."""
    return _looks_like_ship_file(fname)


async def handle_ship_document(msg: Message, state: FSMContext, stored_path: Path, fname: str) -> None:
    """Обработать уже скачанный ship-файл. Вызывается из upload.py."""
    try:
        parsed = parse_ship_file(stored_path, original_name=fname)
    except Exception as e:
        await msg.answer(f"⚠ Не распарсил {fname}: <code>{e}</code>")
        return

    # Покажем что нашли
    total_qty = sum(it.qty for it in parsed.items)
    await msg.answer(
        f"📥 <b>{parsed.marketplace.upper()}</b> «{parsed.cluster_name}»\n"
        f"Позиций: {len(parsed.items)}, всего {total_qty} шт"
    )

    # К каким открытым заявкам можно привязать? Фильтр: только заявки с тем же
    # marketplace (WB-файл к WB-заявке, Ozon-файл к Ozon-заявке).
    # Заявки без items (свежие пустые) тоже подходят.
    file_mp = parsed.marketplace  # 'wb' | 'ozon'
    open_summaries: List[tuple] = []  # (rid, state, n_items)
    tg_id_attach = msg.from_user.id if msg.from_user else 0
    with db_session() as session:
        reqs = list_shipment_requests(session, user_id=tg_id_attach, limit=10)
        for r in reqs:
            if r.state not in {"draft", "planning"}:
                continue
            mps_in_req = {it.marketplace for it in r.items}
            if mps_in_req and file_mp not in mps_in_req:
                # Заявка для другого MP — не предлагаем
                continue
            if len(mps_in_req) > 1:
                # Уже смешанная — не плодим
                continue
            open_summaries.append((r.id, r.state, len(r.items)))

    # Сохраним parsed во FSM
    await state.update_data(
        ship_file_path=str(stored_path),
        ship_file_name=fname,
    )

    if not open_summaries:
        # Создаём сразу новую заявку. Для Ozon-файла предварительно спрашиваем
        # тип поставки (фиксируется в req навсегда).
        if parsed.marketplace == "ozon":
            await state.update_data(up_otype_kind="single")
            await _ask_ozon_type_for_new(
                msg, state,
                header=f"📥 OZON «{parsed.cluster_name}» · {len(parsed.items)} позиций",
            )
            return
        with db_session() as session:
            req = create_shipment_request(session, source_file=fname)
            result = attach_ship_file(session, req.id, parsed)
            rid = req.id
        await _send_attach_result(msg, rid, result)
        return

    # Спросим: новую или к существующей?
    rows = [[InlineKeyboardButton(text="➕ Новая поставка", callback_data="ship_new")]]
    for rid, rstate, n_items in open_summaries:
        rows.append([InlineKeyboardButton(
            text=f"➤ #{rid} ({n_items} строк, {rstate})",
            callback_data=f"ship_attach:{rid}",
        )])
    rows.append([InlineKeyboardButton(text="✖ Отмена", callback_data="cancel")])
    await state.set_state(ShipPick.pick_request)
    await msg.answer(
        "К какой поставке привязать?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(ShipPick.pick_request, F.data == "ship_new")
async def cb_ship_new(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    fname = data.get("ship_file_name", "")
    fpath = data.get("ship_file_path", "")

    try:
        parsed = parse_ship_file(Path(fpath), original_name=fname)
    except Exception as e:
        await state.clear()
        await cb.answer(f"Ошибка: {e}", show_alert=True)
        return

    # Для Ozon — сначала спрашиваем тип поставки.
    if parsed.marketplace == "ozon":
        await state.update_data(up_otype_kind="single")
        await cb.answer()
        if cb.message:
            await _ask_ozon_type_for_new(
                cb.message, state,
                header=f"📥 OZON «{parsed.cluster_name}» · {len(parsed.items)} позиций",
            )
        return

    await state.clear()
    with db_session() as session:
        req = create_shipment_request(session, source_file=fname)
        result = attach_ship_file(session, req.id, parsed)
        rid = req.id

    await cb.answer("Создано")
    if cb.message:
        await _send_attach_result(
            cb.message, rid, result,
            header=f"✅ Создана новая поставка #{rid}",
        )


@router.callback_query(ShipPick.pick_request, F.data.startswith("ship_attach:"))
async def cb_ship_attach(cb: CallbackQuery, state: FSMContext) -> None:
    rid = int(cb.data.split(":", 1)[1])
    data = await state.get_data()
    fname = data.get("ship_file_name", "")
    fpath = data.get("ship_file_path", "")
    await state.clear()

    try:
        parsed = parse_ship_file(Path(fpath), original_name=fname)
    except Exception as e:
        await cb.answer(f"Ошибка: {e}", show_alert=True)
        return

    with db_session() as session:
        result = attach_ship_file(session, rid, parsed)

    await cb.answer("Привязано")
    if cb.message:
        await _send_attach_result(
            cb.message, rid, result,
            header=f"✅ Привязано к поставке #{rid}",
        )


async def _send_attach_result(
    msg_or_cb_msg: Message, rid: int, result, header: Optional[str] = None,
) -> None:
    """Показать результат привязки файла к заявке.

    Если передан `header` — он встаёт первой строкой (например «✅ Создана заявка #N»),
    раньше эти заголовки слались отдельным safe_edit_or_answer'ом → было два сообщения
    подряд. Теперь — один edit-call. На сообщении-файле edit невозможен — fallback в
    answer() сам сработает.
    """
    lines: List[str] = []
    if header:
        lines.append(header)
        lines.append("")
    lines.append(
        f"📦 Поставка #{rid} — добавлено <b>{result.items_added}</b> строк "
        f"({result.cluster} / {result.marketplace.upper()})"
    )
    lines.append(f"✅ В каталоге: {result.matched}")
    if result.unmatched_articles:
        lines.append(f"⚠ Не нашёл в каталоге: {len(result.unmatched_articles)}")
        for a in result.unmatched_articles[:15]:
            lines.append(f"  <code>{a}</code>")
        if len(result.unmatched_articles) > 15:
            lines.append(f"  …ещё {len(result.unmatched_articles) - 15}")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Открыть поставку", callback_data=f"ship_open:{rid}")],
        [InlineKeyboardButton(text="📎 Привязать ещё файл", callback_data="ship_more")],
    ])
    # До ~15 unmatched-строк × ~30 симв ≈ <1000 символов — гарантированно влезает.
    # send_long тут не нужен и плодил бы новое сообщение.
    await safe_edit_or_answer(msg_or_cb_msg, "\n".join(lines), reply_markup=kb)


@router.callback_query(F.data.startswith("ship_open:"))
async def cb_ship_open(cb: CallbackQuery) -> None:
    rid = int(cb.data.split(":", 1)[1])
    # При первом открытии карточки тянем статусы Ozon мягко: если кэш свежий —
    # ничего не дёргаем, иначе один вызов API. Не блокируем UI на ошибках.
    try:
        from src.services.ozon_supply_status_service import refresh_supply_status, is_cache_fresh
        if not is_cache_fresh(rid):
            tg_id = current_user_id_from(cb)
            cli = None
            if tg_id is not None:
                with db_session() as session:
                    cli = get_ozon_client_for(session, tg_id)
            if cli is not None:
                with db_session() as session:
                    await refresh_supply_status(session, cli, rid, force=False)
    except Exception as e:
        logger.warning("ship_open: status refresh failed rid=%s: %s", rid, e)

    with db_session() as session:
        tg_id_open = cb.from_user.id if cb.from_user else 0
        req = get_shipment_request(session, rid, user_id=tg_id_open)
        if not req:
            await cb.answer("Не найдена", show_alert=True)
            return
        text, kb = _render_request_card(req)
    await cb.answer()
    if cb.message:
        await safe_edit_or_answer(cb.message, text, reply_markup=kb)


@router.callback_query(F.data == "ship_new_template")
async def cb_ship_new_template(cb: CallbackQuery) -> None:
    await cb.answer("Готовлю шаблон…")
    if not cb.message:
        return
    try:
        from src.generators.new_request_template import generate_template
        tg_id = current_user_id_from(cb)
        if tg_id is None:
            return
        with db_session() as session:
            cli = get_ozon_client_for(session, tg_id)
        if cli is None:
            await cb.message.answer(_NO_OZON_KEYS_MSG_SHIP)
            return
        clusters = await cli.cluster_list()
        cluster_names = [c.get("name") or c.get("cluster_name") or "" for c in clusters]
        cluster_names = [n for n in cluster_names if n]
        if not cluster_names:
            await cb.message.answer(
                "⚠ Не удалось получить список кластеров Ozon. Попробуй ещё раз через минуту."
            )
            return
        user_id = cb.from_user.id if cb.from_user else 0
        with db_session() as session:
            data = generate_template(session, user_id, cluster_names)
            from src.db.models import OzonProduct
            n_products = session.query(OzonProduct).filter(
                OzonProduct.user_id == user_id
            ).count()
        from aiogram.types import BufferedInputFile
        await cb.message.answer_document(
            BufferedInputFile(data, filename="Новая_поставка_шаблон.xlsx"),
            caption=(
                f"📋 <b>Шаблон новой поставки</b>\n\n"
                f"Артикулов: <b>{n_products}</b>, кластеров: <b>{len(cluster_names)}</b>.\n"
                "Заполни количество в нужных кластерах и пришли файл обратно — "
                "бот создаст одну поставку со всеми направлениями.\n\n"
                "<i>Пустые ячейки и нули игнорируются.</i>"
            ),
        )
    except Exception as e:
        logger.exception("ship_new_template failed: %s", e)
        await cb.message.answer(f"⚠ Ошибка: <code>{type(e).__name__}: {e}</code>")


@router.message(Command("clear_drafts"))
async def cmd_clear_drafts(msg: Message, _tg_id: int | None = None) -> None:
    """Массовая чистка: отменить в Ozon все supply orders в статусе DATA_FILLING +
    удалить из БД все заявки, у которых нет активных booked items.

    Не трогает поставки в более продвинутых статусах (READY_TO_SUPPLY и далее).
    Доступно только админам (ADMIN_USER_IDS).
    `_tg_id` — explicit override (callback-обёртки передают cb.from_user.id)."""
    from src.config import ADMIN_USER_IDS
    actor = _tg_id if _tg_id is not None else (msg.from_user.id if msg.from_user else None)
    if not actor or actor not in ADMIN_USER_IDS:
        await msg.answer("⛔ Команда доступна только админам.")
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="⚠ Да, чистить все черновики",
            callback_data="clear_drafts:yes",
        )],
        [InlineKeyboardButton(text="✖ Отмена", callback_data="menu:home")],
    ])
    await msg.answer(
        "🧹 <b>Массовая чистка черновиков</b>\n\n"
        "Будет сделано:\n"
        "1. В Ozon ЛК — отменены все поставки в статусе <b>📝 Заполнение данных</b>.\n"
        "2. В боте — удалены все заявки без активных поставок "
        "(черновики + те у кого всё было в DATA_FILLING и отменилось).\n\n"
        "Поставки в продвинутых статусах (Готово к отгрузке, В пути, Принято) "
        "<b>НЕ трогаются</b>.",
        reply_markup=kb,
    )


@router.callback_query(F.data == "clear_drafts:yes")
async def cb_clear_drafts(cb: CallbackQuery) -> None:
    from src.config import ADMIN_USER_IDS
    if cb.from_user.id not in ADMIN_USER_IDS:
        await cb.answer("⛔ Только админам", show_alert=True)
        return
    await cb.answer("Чищу…")
    if not cb.message:
        return

    tg_id = cb.from_user.id
    from src.services.ozon_supply_status_service import (
        cancel_supply_orders, refresh_supply_status,
    )
    with db_session() as _s:
        cli = get_ozon_client_for(_s, tg_id)
    if cli is None:
        await cb.message.answer(_NO_OZON_KEYS_MSG_SHIP)
        return

    # Шаг 1: освежить статусы по всем заявкам с booked items.
    # Чистим ТОЛЬКО свои черновики — каждый админ имеет свой кабинет.
    progress = await cb.message.answer("⏳ Освежаю статусы Ozon…")
    with db_session() as session:
        from src.db.models import ShipmentRequest
        all_reqs = session.query(ShipmentRequest).filter(
            ShipmentRequest.user_id == tg_id
        ).all()
        rids_with_booked = [
            r.id for r in all_reqs
            if any(it.marketplace == "ozon" and it.booked_supply_id for it in r.items)
        ]
    for rid in rids_with_booked:
        try:
            with db_session() as s:
                await refresh_supply_status(s, cli, rid, force=True)
        except Exception as e:
            logger.warning("clear_drafts: refresh failed rid=%s: %s", rid, e)

    # Шаг 2: собираем order_ids в DATA_FILLING — отменяем через API.
    with db_session() as session:
        oids_to_cancel: List[int] = []
        for r in session.query(ShipmentRequest).filter(
            ShipmentRequest.user_id == ALLOWED_USER_ID
        ).all():
            for it in r.items:
                if (
                    it.marketplace == "ozon"
                    and it.booked_supply_id
                    and (it.ozon_supply_status or "").upper() == "DATA_FILLING"
                ):
                    try:
                        oids_to_cancel.append(int(it.booked_supply_id))
                    except (TypeError, ValueError):
                        pass
        oids_to_cancel = list(set(oids_to_cancel))

    cancel_results = []
    if oids_to_cancel:
        try:
            await progress.edit_text(
                f"⏳ Отменяю {len(oids_to_cancel)} поставок в Ozon…"
            )
        except Exception:
            pass
        cancel_results = await cancel_supply_orders(None, cli, oids_to_cancel)
        # После отмены — refresh, чтобы обнулить booked_* в БД.
        for rid in rids_with_booked:
            try:
                with db_session() as s:
                    await refresh_supply_status(s, cli, rid, force=True)
            except Exception:
                pass

    # Шаг 3: удаляем из БД все заявки без активных booked items.
    deleted_rids: List[int] = []
    with db_session() as session:
        for r in session.query(ShipmentRequest).filter(
            ShipmentRequest.user_id == ALLOWED_USER_ID
        ).all():
            has_active = any(
                it.booked_supply_id for it in r.items
            )
            if not has_active:
                deleted_rids.append(r.id)
                session.delete(r)

    cancelled_ok = sum(1 for r in cancel_results if r["cancelled"])
    cancelled_fail = len(cancel_results) - cancelled_ok
    summary = [
        "🧹 <b>Очистка завершена</b>",
        f"📝 Отменено в Ozon: <b>{cancelled_ok}</b>",
    ]
    if cancelled_fail:
        summary.append(f"⚠ Не удалось отменить: {cancelled_fail}")
    summary.append(f"🗑 Удалено заявок из БД: <b>{len(deleted_rids)}</b>")
    if deleted_rids:
        summary.append(f"   #{', #'.join(str(x) for x in deleted_rids[:30])}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Мои поставки", callback_data="menu:ships")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:home")],
    ])
    try:
        await progress.edit_text("\n".join(summary), reply_markup=kb)
    except Exception:
        await cb.message.answer("\n".join(summary), reply_markup=kb)


@router.callback_query(F.data.startswith("ship_cancel_oz:"))
async def cb_ship_cancel_oz(cb: CallbackQuery) -> None:
    parts = cb.data.split(":")
    rid = int(parts[1])
    confirm = len(parts) > 2 and parts[2] == "yes"
    if not confirm:
        await cb.answer()
        if cb.message:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="⚠ Да, отменить все Ozon-поставки",
                    callback_data=f"ship_cancel_oz:{rid}:yes",
                )],
                [InlineKeyboardButton(text="◀ Назад", callback_data=f"ship_open:{rid}")],
            ])
            await safe_edit_or_answer(
                cb.message,
                f"⚠ Отменить ВСЕ Ozon-поставки заявки #{rid}?\n\n"
                "Они исчезнут из Ozon ЛК (статус CANCELLED) и в боте отвяжутся "
                "от направлений — сможешь пробронировать заново.",
                reply_markup=kb,
            )
        return

    await cb.answer("Отменяю…")
    try:
        from src.services.ozon_supply_status_service import cancel_supply_orders, refresh_supply_status
        tg_id = current_user_id_from(cb)
        if tg_id is None:
            return
        with db_session() as session:
            cli = get_ozon_client_for(session, tg_id)
        if cli is None:
            if cb.message:
                await cb.message.answer(_NO_OZON_KEYS_MSG_SHIP)
            return
        with db_session() as session:
            req = get_shipment_request(session, rid, user_id=tg_id)
            if not req:
                return
            oids = []
            for it in req.items:
                if it.marketplace == "ozon" and it.booked_supply_id:
                    try:
                        oids.append(int(it.booked_supply_id))
                    except (TypeError, ValueError):
                        pass
            oids = list(set(oids))
        if not oids:
            if cb.message:
                await cb.message.answer("Нет забронированных Ozon-поставок.")
            return
        results = await cancel_supply_orders(None, cli, oids)
        # Перезагрузим статусы — обнулит CANCELLED items.
        with db_session() as session:
            await refresh_supply_status(session, cli, rid, force=True)
            req = get_shipment_request(session, rid, user_id=tg_id)
            text, kb = _render_request_card(req)
        ok = sum(1 for r in results if r["cancelled"])
        fails = [(r["order_id"], r["error"]) for r in results if not r["cancelled"]]
        summary = f"🗑 Отменено: {ok}/{len(results)}"
        if fails:
            summary += "\n" + "\n".join(
                f"  ❌ {oid}: {err or 'не подтвердилось'}" for oid, err in fails[:5]
            )
        if cb.message:
            await cb.message.answer(summary)
            await safe_edit_or_answer(cb.message, text, reply_markup=kb)
    except Exception as e:
        logger.exception("ship_cancel_oz failed rid=%s: %s", rid, e)
        if cb.message:
            await cb.message.answer(f"⚠ Ошибка: <code>{type(e).__name__}: {e}</code>")


@router.callback_query(F.data.startswith("ship_refresh_oz:"))
async def cb_ship_refresh_oz(cb: CallbackQuery) -> None:
    rid = int(cb.data.split(":", 1)[1])
    await cb.answer("Обновляю…")
    try:
        from src.services.ozon_supply_status_service import refresh_supply_status
        tg_id = current_user_id_from(cb)
        if tg_id is None:
            return
        with db_session() as session:
            cli = get_ozon_client_for(session, tg_id)
        if cli is None:
            if cb.message:
                await cb.message.answer(_NO_OZON_KEYS_MSG_SHIP)
            return
        with db_session() as session:
            n = await refresh_supply_status(session, cli, rid, force=True)
    except Exception as e:
        logger.exception("ship_refresh_oz failed rid=%s: %s", rid, e)
        if cb.message:
            await cb.message.answer(f"⚠ Не получилось обновить: <code>{type(e).__name__}: {e}</code>")
        return

    with db_session() as session:
        req = get_shipment_request(session, rid, user_id=tg_id)
        if not req:
            return
        text, kb = _render_request_card(req)
    if cb.message:
        await safe_edit_or_answer(cb.message, text, reply_markup=kb)


async def _create_zip_together_request(
    msg: Message, state: FSMContext, paths: List[Path], zip_name: str,
    *, otype: Optional[str],
) -> None:
    """Собрать единую заявку из всех xlsx в zip. Если otype передан — фиксируем
    его в req.ozon_supply_type. Прогресс — в одну «сардельку»."""
    from src.bot.helpers import progress_start, progress_add, progress_reset
    await progress_reset(state)
    tg_id_zip = msg.from_user.id if msg.from_user else 0
    with db_session() as session:
        req = create_shipment_request(session, source_file=zip_name, user_id=tg_id_zip)
        if otype:
            req.ozon_supply_type = otype
        rid = req.id
    header_extra = ""
    if otype:
        label = "Прямая" if otype == "direct" else "Кросс-докинг"
        header_extra = f" · {label}"
    await progress_start(
        msg, state,
        f"📋 Создана единая поставка <b>#{rid}</b>{header_extra} на {len(paths)} файлов.",
    )
    ok, errs = 0, 0
    for path in paths:
        inner_name = path.name
        if not _looks_like_ship_file(inner_name):
            await progress_add(msg, state, f"❔ <code>{inner_name}</code>: пропуск (не ship-выгрузка).")
            continue
        try:
            parsed = parse_ship_file(path)
            with db_session() as session:
                attach_ship_file(session, rid, parsed, user_id=tg_id_zip)
            ok += 1
            await progress_add(msg, state, f"  ✅ <code>{inner_name}</code>")
        except Exception as e:
            errs += 1
            logger.exception("zip together: failed %s", inner_name)
            await progress_add(
                msg, state,
                f"  ⚠ <code>{inner_name}</code>: {type(e).__name__}: {str(e)[:200]}"
            )
    await progress_add(msg, state, f"\n📦 Готово: {ok} ок, {errs} с ошибками.")
    with db_session() as session:
        req = get_shipment_request(session, rid, user_id=tg_id_zip)
        if req:
            text, kb = _render_request_card(req)
            await msg.answer(text, reply_markup=kb)


async def _ask_ozon_type_for_new(msg: Message, state: FSMContext, *, header: str) -> None:
    """Показать экран выбора типа Ozon-поставки (новый шаг wizard'а до создания заявки).

    Контекст (что делать после выбора) уже должен быть сохранён в state — см.
    `cb_up_otype` за разветвлением логики.
    """
    rows = [
        [InlineKeyboardButton(text="🚚 Прямая (на РФЦ)", callback_data="up_otype:d")],
        [InlineKeyboardButton(text="🔀 Кросс-докинг (хаб)", callback_data="up_otype:c")],
        [InlineKeyboardButton(text="✖ Отмена", callback_data="up_otype:x")],
    ]
    await state.set_state(ShipNewType.pick_otype)
    await msg.answer(
        f"{header}\n\n<b>Тип поставки Ozon?</b>\nЗафиксируется навсегда для этой поставки.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(ShipNewType.pick_otype, F.data.startswith("up_otype:"))
async def cb_up_otype(cb: CallbackQuery, state: FSMContext) -> None:
    """Юзер выбрал тип Ozon-поставки на новом wizard-шаге. Создаём заявку с типом."""
    code = cb.data.split(":", 1)[1]
    data = await state.get_data()
    kind = data.get("up_otype_kind")  # "single" | "zip"
    await state.clear()

    if code == "x":
        await cb.answer("Отменено")
        if cb.message:
            await safe_edit_or_answer(cb.message, "✖ Создание поставки отменено.")
        return

    otype = "direct" if code == "d" else "cross"
    label = "Прямая" if otype == "direct" else "Кросс-докинг"
    await cb.answer(f"Тип: {label}")
    if not cb.message:
        return

    if kind == "single":
        fname = data.get("ship_file_name", "")
        fpath = data.get("ship_file_path", "")
        try:
            parsed = parse_ship_file(Path(fpath), original_name=fname)
        except Exception as e:
            await safe_edit_or_answer(cb.message, f"⚠ Не распарсил {fname}: <code>{e}</code>")
            return
        with db_session() as session:
            req = create_shipment_request(session, source_file=fname)
            req.ozon_supply_type = otype
            result = attach_ship_file(session, req.id, parsed)
            rid = req.id
        await _send_attach_result(
            cb.message, rid, result,
            header=f"✅ Создана поставка <b>#{rid}</b> · {label}",
        )
        return

    if kind == "zip":
        paths = [Path(p) for p in (data.get("up_otype_zip_paths") or [])]
        zip_name = data.get("up_otype_zip_name") or "zip"
        if not paths:
            await safe_edit_or_answer(cb.message, "⚠ Пути zip-файлов потеряны, начни заново.")
            return
        await _create_zip_together_request(cb.message, state, paths, zip_name, otype=otype)
        return


@router.callback_query(F.data.startswith("ship_pick_fmt:"))
async def cb_ship_pick_fmt(cb: CallbackQuery, state: FSMContext) -> None:
    """Экран выбора формата (коробами/паллетами) перед запуском wizard'а."""
    parts = cb.data.split(":")
    rid = int(parts[1])
    # ship_pick_fmt:rid:box / :pallet — финальный выбор
    if len(parts) == 3:
        chosen = parts[2]
        if chosen not in ("box", "pallet"):
            await cb.answer("Битый callback", show_alert=True)
            return
        fmt = "BOX" if chosen == "box" else "PALLET"
        tg_id_fmt = cb.from_user.id if cb.from_user else 0
        with db_session() as session:
            req = get_shipment_request(session, rid, user_id=tg_id_fmt)
            if not req:
                await cb.answer("Не найдена", show_alert=True)
                return
            req.cargo_format = fmt
            otype = req.ozon_supply_type
        mode = "cross" if otype == "cross" else "direct"
        await cb.answer(f"Формат: {'коробами' if fmt == 'BOX' else 'паллетами'}")
        # Запускаем стандартный wizard как при тапе «🚀 Создать поставку Ozon»
        from src.bot.handlers.ozon_book import _start_ozon_book_wizard, _wizard_acquire, _wizard_release
        if not _wizard_acquire(rid):
            if cb.message:
                await cb.message.answer(
                    f"⏳ Ozon-мастер для поставки #{rid} уже запущен. Подожди завершения."
                )
            return
        if cb.message:
            try:
                await cb.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            try:
                launched = await _start_ozon_book_wizard(cb.message, state, rid, mode=mode)
            except Exception:
                _wizard_release(rid)
                raise
            if not launched:
                _wizard_release(rid)
        else:
            _wizard_release(rid)
        return

    # Первый тап — показываем выбор.
    await cb.answer()
    if not cb.message:
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Коробами",
                              callback_data=f"ship_pick_fmt:{rid}:box")],
        [InlineKeyboardButton(text="🏗 Паллетами",
                              callback_data=f"ship_pick_fmt:{rid}:pallet")],
        [InlineKeyboardButton(text="◀ Назад", callback_data=f"ship_open:{rid}")],
    ])
    await safe_edit_or_answer(
        cb.message,
        f"📦 <b>Поставка #{rid}</b>\n\n"
        "Как будешь грузить эту поставку?\n\n"
        "<b>📦 Коробами</b> — каждый короб опишется отдельно (короб № → SKU → qty).\n"
        "<b>🏗 Паллетами</b> — палеты с описью (палет № → SKU → qty).\n\n"
        "<i>Эта пометка нужна для генерации правильной описи. "
        "В Ozon API она пока не отправляется — только для бота.</i>",
        reply_markup=kb,
    )


@router.callback_query(F.data.startswith("ship_set_otype:"))
async def cb_ship_set_otype(cb: CallbackQuery) -> None:
    """Один раз зафиксировать тип Ozon-поставки (direct/cross) и перерисовать карточку."""
    parts = cb.data.split(":")
    if len(parts) != 3:
        await cb.answer("Битый callback", show_alert=True)
        return
    rid = int(parts[1])
    code = parts[2]
    if code not in ("d", "c"):
        await cb.answer("Неизвестный тип", show_alert=True)
        return
    otype = "direct" if code == "d" else "cross"
    tg_id_setotype = cb.from_user.id if cb.from_user else 0
    with db_session() as session:
        req = get_shipment_request(session, rid, user_id=tg_id_setotype)
        if not req:
            await cb.answer("Не найдена", show_alert=True)
            return
        if req.ozon_supply_type and req.ozon_supply_type != otype:
            await cb.answer("Тип уже зафиксирован — изменить нельзя", show_alert=True)
            return
        req.ozon_supply_type = otype
        text, kb = _render_request_card(req)
    label = "Прямая" if otype == "direct" else "Кросс-докинг"
    await cb.answer(f"Тип: {label}")
    if cb.message:
        await safe_edit_or_answer(cb.message, text, reply_markup=kb)


async def _render_ship_delete_picker(
    target: Message, state: FSMContext, *, tg_id: int, edit: bool,
) -> None:
    """Экран multi-select удаления заявок. Состояние выбора — в state[delpicks]."""
    data = await state.get_data()
    picks = set(data.get("delpicks") or [])
    with db_session() as session:
        reqs = list_shipment_requests(session, user_id=tg_id, limit=50)
        items_visible = [r for r in reqs if r.state not in {"closed", "cancelled"}]
        # Кэшируем display для UI.
        items_data = [
            (r.id, _compute_display_label(r),
             next(iter({(it.marketplace, it.cluster) for it in r.items}))[1]
                if len({(it.marketplace, it.cluster) for it in r.items}) == 1
                else f"{len({(it.marketplace, it.cluster) for it in r.items})} напр.",
             r.created_at.strftime("%d.%m"))
            for r in items_visible
        ]

    if not items_data:
        text = "🚚 Нет заявок для удаления."
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀ К списку", callback_data="menu:ships")],
        ])
        if edit:
            await safe_edit_or_answer(target, text, reply_markup=kb)
        else:
            await target.answer(text, reply_markup=kb)
        return

    rows: List[List[InlineKeyboardButton]] = []
    for rid, label_state, cluster_part, date_s in items_data:
        mark = "✅" if rid in picks else "▫"
        rows.append([InlineKeyboardButton(
            text=f"{mark} #{rid} · {cluster_part} · {date_s}"[:55],
            callback_data=f"delpick:{rid}",
        )])
    if picks:
        rows.append([InlineKeyboardButton(
            text=f"🗑 Удалить выбранные ({len(picks)})",
            callback_data="delconfirm",
        )])
    rows.append([InlineKeyboardButton(text="◀ К списку", callback_data="delcancel")])
    text = (
        "🗑 <b>Удаление заявок</b>\n\n"
        f"Тапни на поставку чтобы отметить её. Выбрано: <b>{len(picks)}</b>.\n\n"
        "При удалении: связанные Ozon-поставки <b>отменяются в ЛК через API</b>, "
        "потом заявка стирается из бота. Поставки в продвинутых статусах "
        "(в пути, принято) бот трогать не будет."
    )
    if edit:
        await safe_edit_or_answer(target, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    else:
        await target.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "ships_delete_picker")
async def cb_ships_delete_picker(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.update_data(delpicks=[])
    if cb.message:
        tg_id = cb.from_user.id if cb.from_user else 0
        await _render_ship_delete_picker(cb.message, state, tg_id=tg_id, edit=True)


@router.callback_query(F.data.startswith("delpick:"))
async def cb_delpick(cb: CallbackQuery, state: FSMContext) -> None:
    rid = int(cb.data.split(":", 1)[1])
    data = await state.get_data()
    picks = set(data.get("delpicks") or [])
    if rid in picks:
        picks.discard(rid)
    else:
        picks.add(rid)
    await state.update_data(delpicks=list(picks))
    await cb.answer()
    if cb.message:
        tg_id = cb.from_user.id if cb.from_user else 0
        await _render_ship_delete_picker(cb.message, state, tg_id=tg_id, edit=True)


@router.callback_query(F.data == "delconfirm")
async def cb_delconfirm(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    picks = list(data.get("delpicks") or [])
    if not picks:
        await cb.answer("Ничего не выбрано", show_alert=True)
        return
    await cb.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"⚠ Да, удалить {len(picks)} заявок",
            callback_data="delconfirm:yes",
        )],
        [InlineKeyboardButton(text="◀ Назад к выбору", callback_data="ships_delete_picker")],
    ])
    if cb.message:
        sample = ", ".join(f"#{r}" for r in sorted(picks)[:20])
        more = f" … и ещё {len(picks) - 20}" if len(picks) > 20 else ""
        await safe_edit_or_answer(
            cb.message,
            f"⚠ <b>Удалить безвозвратно?</b>\n\n"
            f"Заявки: {sample}{more}\n\n"
            "Связанные Ozon-поставки в ЛК <b>останутся как есть</b>.",
            reply_markup=kb,
        )


@router.callback_query(F.data == "delconfirm:yes")
async def cb_delconfirm_yes(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    picks = list(data.get("delpicks") or [])
    tg_id = cb.from_user.id if cb.from_user else 0
    deleted = 0
    with db_session() as session:
        for rid in picks:
            r = get_shipment_request(session, rid, user_id=tg_id)
            if r:
                session.delete(r)
                deleted += 1
    await state.update_data(delpicks=[])
    await cb.answer(f"Удалено: {deleted}")
    if cb.message:
        await _render_ship_list(cb.message, tg_id=tg_id, edit=True)


@router.callback_query(F.data == "delcancel")
async def cb_delcancel(cb: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(delpicks=[])
    await cb.answer()
    if cb.message:
        tg_id = cb.from_user.id if cb.from_user else 0
        await _render_ship_list(cb.message, tg_id=tg_id, edit=True)


@router.callback_query(F.data == "ship_more")
async def cb_ship_more(cb: CallbackQuery) -> None:
    await cb.answer("Жду файл…")
    if cb.message:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀ К списку заявок", callback_data="menu:ships")],
        ])
        await safe_edit_or_answer(
            cb.message,
            "📎 Кинь следующий xlsx-файл выгрузки — добавлю в текущую поставку "
            "или создам новую.",
            reply_markup=kb,
        )


# ── /ship_plan — этап 2: даты + кросс-док ────────────────────────────────────

from src.bot.keyboards import kb_dates_picker, kb_hours_picker  # переиспользуем календарь и time-picker


@router.message(Command("ship_plan"))
async def cmd_ship_plan(msg: Message, command: CommandObject, state: FSMContext) -> None:
    try:
        rid = int((command.args or "").strip())
    except ValueError:
        await msg.answer("Использование: <code>/ship_plan ID</code>")
        return
    tg_id_plan = msg.from_user.id if msg.from_user else 0
    await _start_plan_wizard(msg, state, rid, tg_id=tg_id_plan)


@router.callback_query(F.data.startswith("ship_plan:"))
async def cb_ship_plan(cb: CallbackQuery, state: FSMContext) -> None:
    rid = int(cb.data.split(":", 1)[1])
    await cb.answer()
    if cb.message:
        tg_id_plan = cb.from_user.id if cb.from_user else 0
        await _start_plan_wizard(cb.message, state, rid, tg_id=tg_id_plan, edit=True)


async def _start_plan_wizard(
    msg: Message, state: FSMContext, rid: int, *, tg_id: int, edit: bool = False,
) -> None:
    """Показать заявку + календарь дат. edit=True редактирует исходное сообщение.

    Если у заявки уже есть target_date_from/to — предзаполняет выбранные галочки
    в календаре (пользователь видит свой прошлый выбор и может его подкорректировать).
    """
    from datetime import date as _date
    with db_session() as session:
        req = get_shipment_request(session, rid, user_id=tg_id)
        if not req:
            await msg.answer(f"Поставка #{rid} не найдена.")
            return
        n_items = len(req.items)
        directions = sorted({(it.marketplace, it.cluster) for it in req.items})

        # Восстанавливаем галочки из target_date_from/to
        today = _date.today()
        preselected: List[int] = []
        if req.target_date_from:
            d_from = req.target_date_from.date()
            d_to = (req.target_date_to or req.target_date_from).date()
            d_cur = d_from
            while d_cur <= d_to:
                off = (d_cur - today).days
                if 0 <= off < 14:
                    preselected.append(off)
                d_cur += timedelta(days=1)

    await state.set_state(ShipPlan.dates)
    await state.update_data(
        ship_plan_rid=rid,
        ship_plan_selected_offsets=preselected,
        ship_plan_directions=[f"{mp}|{cl}" for mp, cl in directions],
        ship_plan_crossdock={},
    )
    text = (
        f"🛠 <b>Планирование поставки #{rid}</b> ({n_items} строк, {len(directions)} направлений)\n\n"
        f"📅 <b>Шаг 1/2.</b> Выбери целевые даты отгрузки (тапом):"
    )
    if preselected:
        text += f"\n<i>Ранее выбрано: {len(preselected)} даты — можешь добавить/убрать тапом.</i>"
    kb = kb_dates_picker(set(preselected), days_ahead=14, min_offset=0)
    if edit:
        await safe_edit_or_answer(msg, text, reply_markup=kb)
    else:
        await msg.answer(text, reply_markup=kb)


@router.callback_query(ShipPlan.dates, F.data == "dp_lock")
async def cb_sp_lock(cb: CallbackQuery) -> None:
    await cb.answer("Дата закрыта.", show_alert=False)


@router.callback_query(ShipPlan.dates, F.data.startswith("dp:"))
async def cb_sp_toggle(cb: CallbackQuery, state: FSMContext) -> None:
    n = int(cb.data.split(":", 1)[1])
    data = await state.get_data()
    selected = set(data.get("ship_plan_selected_offsets", []))
    if n in selected:
        selected.remove(n)
    else:
        selected.add(n)
    await state.update_data(ship_plan_selected_offsets=sorted(selected))
    await cb.answer()
    if cb.message:
        try:
            await cb.message.edit_reply_markup(
                reply_markup=kb_dates_picker(selected, days_ahead=14, min_offset=0)
            )
        except Exception:
            pass


@router.callback_query(ShipPlan.dates, F.data == "dp_skip")
async def cb_sp_skip(cb: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(ship_plan_target_date_from=None, ship_plan_target_date_to=None)
    await cb.answer("Без целевых дат")
    if cb.message:
        await _ask_crossdock_mode(cb.message, state)


@router.callback_query(ShipPlan.dates, F.data == "dp_ok")
async def cb_sp_confirm_dates(cb: CallbackQuery, state: FSMContext) -> None:
    """Подтверждение дат → переход на time-picker (часы суток)."""
    from datetime import date as _date, timedelta
    data = await state.get_data()
    selected = sorted(data.get("ship_plan_selected_offsets", []))
    if not selected:
        await cb.answer("Выбери хотя бы одну дату или нажми ⏭", show_alert=True)
        return
    rid = data["ship_plan_rid"]
    today = _date.today()
    dates = [today + timedelta(days=n) for n in selected]

    # Предзаполняем выбор часов из заявки если был раньше — юзер видит свой
    # прошлый выбор и может скорректировать (как и для дат).
    preselected_hours: List[int] = []
    tg_id_hrs = cb.from_user.id if cb.from_user else 0
    with db_session() as session:
        req = get_shipment_request(session, rid, user_id=tg_id_hrs)
        if req and req.target_hours_json:
            preselected_hours = list(req.target_hours_json)

    await state.update_data(
        ship_plan_target_dates_iso=[d.isoformat() for d in dates],
        ship_plan_selected_hours=preselected_hours,
    )
    await state.set_state(ShipPlan.hours)
    await cb.answer()
    if cb.message:
        n_dates = len(dates)
        dates_short = ", ".join(f"{d.day:02d}.{d.month:02d}" for d in dates[:6])
        if n_dates > 6:
            dates_short += f", …+{n_dates - 6}"
        hours_hint = ""
        if preselected_hours:
            hours_hint = (
                "\n<i>Восстановил твой прошлый выбор часов — поправь если нужно.</i>"
            )
        await safe_edit_or_answer(
            cb.message,
            f"🕒 <b>Шаг 2/2.</b> Выбери часы отгрузки (тапом, можно несколько).\n"
            f"📅 Дат: {n_dates} ({dates_short})\n\n"
            f"«🎲 <b>Любое время</b>» — без фильтра, бот возьмёт самый ранний слот в день.\n"
            f"Иначе тапни конкретные часы — слоты вне окна будут отфильтрованы."
            + hours_hint,
            reply_markup=kb_hours_picker(set(preselected_hours)),
        )


@router.callback_query(ShipPlan.hours, F.data.startswith("hp:"))
async def cb_sp_hp_toggle(cb: CallbackQuery, state: FSMContext) -> None:
    """Тап на час — toggle selected."""
    try:
        n = int(cb.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await cb.answer("Битый callback", show_alert=True)
        return
    data = await state.get_data()
    sel = set(data.get("ship_plan_selected_hours") or [])
    if n in sel:
        sel.discard(n)
    else:
        sel.add(n)
    await state.update_data(ship_plan_selected_hours=sorted(sel))
    await cb.answer()
    if cb.message:
        try:
            await cb.message.edit_reply_markup(reply_markup=kb_hours_picker(sel))
        except Exception:
            pass


@router.callback_query(ShipPlan.hours, F.data == "hp_any")
async def cb_sp_hp_any(cb: CallbackQuery, state: FSMContext) -> None:
    """Любое время — сохраняем без часового фильтра."""
    await _finalize_plan_with_hours(cb, state, hours=None)


@router.callback_query(ShipPlan.hours, F.data == "hp_ok")
async def cb_sp_hp_ok(cb: CallbackQuery, state: FSMContext) -> None:
    """Подтвердить выбранные часы."""
    data = await state.get_data()
    sel = sorted(data.get("ship_plan_selected_hours") or [])
    if not sel:
        await cb.answer(
            "Тапни хотя бы один час или нажми «🎲 Любое время»",
            show_alert=True,
        )
        return
    await _finalize_plan_with_hours(cb, state, hours=sel)


async def _finalize_plan_with_hours(
    cb: CallbackQuery, state: FSMContext, hours: Optional[List[int]],
) -> None:
    """Сохранить даты + часы в БД и показать карточку заявки.

    hours=None → «любое время» (target_hours_json=NULL, фильтра нет).
    hours=[9,10,11] → слоты только в эти часы старта.
    """
    from datetime import date as _date, datetime as _dt
    data = await state.get_data()
    rid = data.get("ship_plan_rid")
    dates_iso = data.get("ship_plan_target_dates_iso") or []
    if not rid or not dates_iso:
        await cb.answer("Состояние потеряно — открой /ship_plan заново", show_alert=True)
        await state.clear()
        return

    dates = sorted(_date.fromisoformat(s) for s in dates_iso)
    d_from = dates[0]
    d_to = dates[-1] if len(dates) > 1 else None
    await state.clear()

    tg_id_fin = cb.from_user.id if cb.from_user else 0
    with db_session() as session:
        req = get_shipment_request(session, rid, user_id=tg_id_fin)
        if not req:
            await cb.answer("Поставка не найдена", show_alert=True)
            return
        req.target_date_from = _dt.fromisoformat(d_from.isoformat())
        req.target_date_to = _dt.fromisoformat(d_to.isoformat()) if d_to else None
        req.target_dates_json = [d.isoformat() for d in dates]
        req.target_hours_json = hours  # NULL = «любое время»
        req.state = "planning"
        text, kb = _render_request_card(req)

    await cb.answer("✅ Сохранено")
    if cb.message:
        await safe_edit_or_answer(cb.message, text, reply_markup=kb)


async def _ask_crossdock_mode(msg: Message, state: FSMContext) -> None:
    """Пока кросс-док отключён в UX — всегда прямая поставка.
    (Текущая реализация — только текстовые метки, не реальный crossdock через API.)
    """
    await state.update_data(ship_plan_crossdock={})
    await _show_confirm(msg, state)


@router.callback_query(ShipPlan.crossdock_mode, F.data == "cdmode:skip")
async def cb_sp_cd_skip(cb: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(ship_plan_crossdock={})
    await cb.answer("Без кросс-дока")
    if cb.message:
        await _show_confirm(cb.message, state)


@router.callback_query(ShipPlan.crossdock_mode, F.data == "cdmode:one")
async def cb_sp_cd_one(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    if cb.message:
        rows = [[InlineKeyboardButton(text=opt, callback_data=f"cdone:{i}")]
                for i, opt in enumerate(CROSSDOCK_OPTIONS)]
        rows.append([InlineKeyboardButton(text="✖ Отмена", callback_data="cancel")])
        await safe_edit_or_answer(
            cb.message,
            "Выбери один кросс-док для всех направлений:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )


@router.callback_query(ShipPlan.crossdock_mode, F.data.startswith("cdone:"))
async def cb_sp_cd_one_pick(cb: CallbackQuery, state: FSMContext) -> None:
    idx = int(cb.data.split(":", 1)[1])
    opt = CROSSDOCK_OPTIONS[idx]
    data = await state.get_data()
    # Применяем кросс-док ТОЛЬКО к Ozon-направлениям
    ozon_dirs = [d for d in data.get("ship_plan_directions", []) if d.startswith("ozon|")]
    crossdock = {d: opt for d in ozon_dirs}
    await state.update_data(ship_plan_crossdock=crossdock)
    await cb.answer("Сохранено")
    if cb.message:
        await safe_edit_or_answer(cb.message, f"🚛 Кросс-док для Ozon: <b>{opt}</b>")
        await _show_confirm(cb.message, state)


@router.callback_query(ShipPlan.crossdock_mode, F.data == "cdmode:each")
async def cb_sp_cd_each(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    if cb.message:
        await _ask_next_direction(cb.message, state)


async def _ask_next_direction(msg: Message, state: FSMContext) -> None:
    """Итерируем только Ozon-направления — WB пропускаем."""
    data = await state.get_data()
    directions = [d for d in data.get("ship_plan_directions", []) if d.startswith("ozon|")]
    crossdock = dict(data.get("ship_plan_crossdock", {}))
    remaining = [d for d in directions if d not in crossdock]
    if not remaining:
        await _show_confirm(msg, state)
        return

    current = remaining[0]
    mp, cl = current.split("|", 1)
    await state.set_state(ShipPlan.crossdock_each_pick)
    await state.update_data(ship_plan_current_direction=current)

    rows = [[InlineKeyboardButton(text=opt, callback_data=f"cdeach:{i}")]
            for i, opt in enumerate(CROSSDOCK_OPTIONS)]
    rows.append([InlineKeyboardButton(text="✖ Отмена", callback_data="cancel")])
    progress = f"({len(directions) - len(remaining) + 1}/{len(directions)})"
    await msg.answer(
        f"🎯 {progress} <b>OZON «{cl}»</b> — какой кросс-док?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(ShipPlan.crossdock_each_pick, F.data.startswith("cdeach:"))
async def cb_sp_cd_each_pick(cb: CallbackQuery, state: FSMContext) -> None:
    idx = int(cb.data.split(":", 1)[1])
    opt = CROSSDOCK_OPTIONS[idx]
    data = await state.get_data()
    current = data.get("ship_plan_current_direction")
    crossdock = dict(data.get("ship_plan_crossdock", {}))
    if current:
        crossdock[current] = opt
    await state.update_data(ship_plan_crossdock=crossdock)
    await cb.answer(f"Записал: {opt[:30]}")
    if cb.message:
        await _ask_next_direction(cb.message, state)


async def _show_confirm(msg: Message, state: FSMContext) -> None:
    data = await state.get_data()
    rid = data["ship_plan_rid"]
    d_from = data.get("ship_plan_target_date_from")
    d_to = data.get("ship_plan_target_date_to")
    crossdock = data.get("ship_plan_crossdock", {})

    date_s = "не указано"
    if d_from:
        date_s = d_from
        if d_to:
            date_s += f" — {d_to}"

    cd_lines = "\n".join(f"  {k}: {v}" for k, v in crossdock.items()) or "  (нет)"
    text = (
        f"✅ <b>Готов сохранить план заявки #{rid}</b>\n\n"
        f"📅 Целевые даты: {date_s}\n\n"
        f"🚛 Кросс-док:\n{cd_lines}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Сохранить", callback_data="ship_plan_save")],
        [InlineKeyboardButton(text="✖ Отмена", callback_data="cancel")],
    ])
    await state.set_state(ShipPlan.confirm)
    await safe_edit_or_answer(msg, text, reply_markup=kb)


@router.callback_query(ShipPlan.confirm, F.data == "ship_plan_save")
async def cb_sp_save(cb: CallbackQuery, state: FSMContext) -> None:
    from datetime import datetime as _dt
    data = await state.get_data()
    rid = data["ship_plan_rid"]
    d_from = data.get("ship_plan_target_date_from")
    d_to = data.get("ship_plan_target_date_to")
    d_list = data.get("ship_plan_target_dates")
    crossdock = data.get("ship_plan_crossdock", {})
    await state.clear()

    tg_id_save = cb.from_user.id if cb.from_user else 0
    with db_session() as session:
        req = get_shipment_request(session, rid, user_id=tg_id_save)
        if not req:
            await cb.answer("Не найдена", show_alert=True)
            return
        if d_from:
            req.target_date_from = _dt.fromisoformat(d_from)
        if d_to:
            req.target_date_to = _dt.fromisoformat(d_to)
        req.target_dates_json = d_list or None
        req.crossdock_warehouses_json = crossdock
        req.state = "planning"

    await cb.answer("✅ План сохранён")
    if cb.message:
        # Показываем обновлённую карточку с кнопками действий
        with db_session() as session:
            req = get_shipment_request(session, rid, user_id=tg_id_save)
            if req:
                text, kb = _render_request_card(req)
                await safe_edit_or_answer(cb.message, text, reply_markup=kb)


# ── /ship_hunt — этап 3a: разведка слотов ────────────────────────────────────

@router.message(Command("ship_hunt"))
async def cmd_ship_hunt(msg: Message, command: CommandObject) -> None:
    try:
        rid = int((command.args or "").strip())
    except ValueError:
        await msg.answer("Использование: <code>/ship_hunt ID</code>")
        return
    await _run_hunt(msg, rid)


@router.callback_query(F.data.startswith("ship_hunt:"))
async def cb_ship_hunt(cb: CallbackQuery) -> None:
    rid = int(cb.data.split(":", 1)[1])
    await cb.answer("Запускаю разведку…")
    if cb.message:
        await _run_hunt(cb.message, rid)


async def _run_hunt(msg: Message, rid: int) -> None:
    from datetime import timedelta as _td
    from src.integrations import OzonClient, WBClient
    from src.services.slot_hunter import hunt_wb, hunt_ozon

    tg_id = current_user_id_from(msg)
    if tg_id is None:
        return

    with db_session() as session:
        req = get_shipment_request(session, rid, user_id=tg_id)
        if not req:
            await msg.answer(f"Поставка #{rid} не найдена.")
            return
        if req.state not in {"planning", "slot_searching", "draft"}:
            await msg.answer(f"Поставка #{rid} в состоянии [{_state_label(req.state)}] — разведка не нужна.")
            return

        # Целевые даты
        target_dates = []
        if req.target_date_from:
            d_cur = req.target_date_from.date()
            d_end = (req.target_date_to or req.target_date_from).date()
            while d_cur <= d_end:
                target_dates.append(d_cur)
                d_cur += _td(days=1)

        # Уникальные направления + товары по кластерам для WB (barcode+qty)
        directions = sorted({(it.marketplace, it.cluster) for it in req.items})
        wb_goods_by_cluster: dict = {}
        for it in req.items:
            if it.marketplace != "wb":
                continue
            p = it.wb_product
            bc = p.barcode_primary if p else None
            if not bc:
                continue
            bucket = wb_goods_by_cluster.setdefault(it.cluster, {})
            bucket[bc] = bucket.get(bc, 0) + it.qty
        if req.state == "draft":
            req.state = "slot_searching"

    if not target_dates:
        await msg.answer("⚠ У поставки не указаны целевые даты — пройди /ship_plan сначала.")
        return

    await msg.answer(
        f"🔍 <b>Разведка слотов для #{rid}</b>\n"
        f"Дат: {len(target_dates)} ({target_dates[0]:%Y-%m-%d} — {target_dates[-1]:%Y-%m-%d})\n"
        f"Направлений: {len(directions)}"
    )

    with db_session() as _s:
        wb_key = get_wb_api_key(_s, tg_id)
        oz_cli = get_ozon_client_for(_s, tg_id)
    wb_cli = WBClient(wb_key) if wb_key else None

    for mp, cluster in directions:
        if mp == "ozon":
            # Ozon хант через API невозможен (timeslot/info глобально лимитирован).
            # Для Ozon — отдельный flow через кнопку «🚀 Создать поставку Ozon» в карточке.
            await msg.answer(
                f"ℹ Ozon «{cluster}»: разведка слотов недоступна. "
                f"Тапни «🚀 Создать поставку Ozon» в карточке — там полный мастер."
            )
            continue

        await msg.answer(f"🔄 Ищу WB «{cluster}»…")
        if not wb_cli:
            await msg.answer("⚠ WB-ключ не настроен — пропускаю WB.")
            continue
        goods_dict = wb_goods_by_cluster.get(cluster) or {}
        goods = [{"barcode": bc, "quantity": qty} for bc, qty in goods_dict.items()]
        try:
            cands, warns = await hunt_wb(wb_cli, cluster, target_dates, goods=goods or None)
        except Exception as e:
            await msg.answer(f"❌ WB ошибка: <code>{str(e)[:300]}</code>")
            continue

        for w in warns:
            await msg.answer(f"⚠ {w}")

        if not cands:
            await msg.answer(
                f"🔴 {mp.upper()} «{cluster}»: подходящих слотов <b>не нашёл</b>.\n"
                f"Можно расширить даты в /ship_plan или подождать."
            )
            continue

        # Плоский список (warehouse × date), отсортирован по логистике/приёмке
        # Кэшируем для пагинации
        flat = []
        for c in cands:
            if not c.slot_date or not c.warehouse_id:
                continue
            flat.append({
                "wid": c.warehouse_id,
                "name": c.warehouse_name,
                "dlv": c.delivery_coef or 0,
                "coef": c.coefficient or 0,
                "date": c.slot_date.isoformat(),
            })
        _HUNT_CACHE[(rid, mp, "flat", cluster)] = flat

        await _render_hunt_page(msg, rid, mp, cluster, page=0)

    await msg.answer(
        f"✅ Разведка завершена для #{rid}.\n"
        "Тапни вариант чтобы сохранить выбор."
    )


@router.callback_query(F.data.startswith("book:"))
async def cb_book_placeholder(cb: CallbackQuery) -> None:
    """Сохраняем выбор склада + даты в БД.
    Для WB — даём прямую ссылку в ЛК (создание поставки через API недоступно).
    Для Ozon — рекомендуем /ozon_book (там работает Draft API)."""
    from datetime import datetime as _dt
    parts = cb.data.split(":")
    if len(parts) < 5:
        await cb.answer("Битый callback", show_alert=True)
        return
    _, rid_s, mp_short, wid_s, ds = parts
    rid = int(rid_s)
    mp = "wb" if mp_short == "w" else "ozon"

    tg_id_pk = cb.from_user.id if cb.from_user else 0
    with db_session() as session:
        req = get_shipment_request(session, rid, user_id=tg_id_pk)
        if not req:
            await cb.answer("Поставка не найдена", show_alert=True)
            return
        affected = 0
        for it in req.items:
            if it.marketplace == mp and it.booked_supply_id is None:
                it.target_warehouse = f"id:{wid_s}" if wid_s != "0" else ""
                if ds and ds != "0":
                    try:
                        it.booked_slot_at = _dt.fromisoformat(ds)
                    except ValueError:
                        pass
                affected += 1

    await cb.answer(f"Записал {affected} позиций")
    if cb.message:
        if mp == "wb":
            url = "https://seller.wildberries.ru/supplies-management/all-supplies"
            rows = [
                [InlineKeyboardButton(text="🌐 Открыть WB ЛК → Поставки", url=url)],
                [InlineKeyboardButton(text="📋 Карточка поставки", callback_data=f"ship_open:{rid}")],
            ]
            await cb.message.answer(
                f"📌 Сохранил выбор WB (склад id={wid_s}, дата {ds}).\n"
                f"Позиций: {affected}.\n\n"
                f"⚠ <b>WB не даёт создать поставку через API</b> — нужно завершить в ЛК:\n"
                f"1. Перейди по кнопке ниже\n"
                f"2. «Создать поставку» → выбери склад с тем же ID\n"
                f"3. Загрузи XLSX из /ship_tz {rid}",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            )
        else:
            await cb.message.answer(
                f"📌 Сохранил выбор Ozon (склад id={wid_s}, дата {ds}). Позиций: {affected}.\n"
                f"Для бронирования через Draft API: <code>/ozon_book {rid}</code>"
            )


_HUNT_PAGE_SIZE = 5


async def _render_hunt_page(msg: Message, rid: int, mp: str, cluster: str, page: int, *, edit: bool = False) -> None:
    """Плоский paginated список (склад × дата) для одного направления.
    edit=True: пытаемся отредактировать существующее сообщение, иначе шлём новое.
    """
    flat = _HUNT_CACHE.get((rid, mp, "flat", cluster))
    if not flat:
        await msg.answer("Кэш слотов истёк — запусти заново «🔍 Подобрать склад WB» в карточке заявки.")
        return

    total = len(flat)
    n_pages = (total + _HUNT_PAGE_SIZE - 1) // _HUNT_PAGE_SIZE
    page = max(0, min(page, n_pages - 1))
    start = page * _HUNT_PAGE_SIZE
    chunk = flat[start:start + _HUNT_PAGE_SIZE]

    lines = [f"🟢 <b>{mp.upper()} «{cluster}»</b> · {total} вариантов · стр. {page + 1}/{n_pages}\n"]
    rows: List[List[InlineKeyboardButton]] = []
    for entry in chunk:
        pct = entry["dlv"]
        lg = "🟢" if pct <= 120 else ("🟡" if pct <= 150 else "🔴")
        pct_s = f"{pct:g}%" if pct else "—"
        try:
            from datetime import date as _date
            d = _date.fromisoformat(entry["date"])
            date_label = d.strftime("%d.%m %a")
        except (ValueError, TypeError):
            date_label = entry["date"]
        btn_text = f"{lg} {entry['name'][:25]} · {date_label} · {pct_s}"
        cb_data = f"book:{rid}:{mp[0]}:{entry['wid']}:{entry['date']}"
        if len(cb_data.encode()) <= 64:
            rows.append([InlineKeyboardButton(text=btn_text[:60], callback_data=cb_data)])

    # Навигация
    nav: List[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            text="◀ Назад", callback_data=f"huntpg:{rid}:{mp[0]}:{_safe_cluster(cluster)}:{page - 1}"))
    if page < n_pages - 1:
        nav.append(InlineKeyboardButton(
            text="Далее ▶", callback_data=f"huntpg:{rid}:{mp[0]}:{_safe_cluster(cluster)}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="📋 К карточке заявки", callback_data=f"ship_open:{rid}")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    text_out = "\n".join(lines)
    if edit:
        try:
            await msg.edit_text(text_out, reply_markup=kb)
            return
        except Exception:
            pass
    await msg.answer(text_out, reply_markup=kb)


def _safe_cluster(cluster: str) -> str:
    """Хеш кластера для callback_data — кириллица съедает много байт."""
    import hashlib
    return hashlib.md5(cluster.encode("utf-8")).hexdigest()[:8]


def _cluster_by_hash(rid: int, mp: str, h: str) -> Optional[str]:
    """Обратный лукап кластера по хешу через _HUNT_CACHE."""
    for key in _HUNT_CACHE:
        if len(key) == 4 and key[0] == rid and key[1] == mp and key[2] == "flat":
            if _safe_cluster(key[3]) == h:
                return key[3]
    return None


@router.callback_query(F.data.startswith("huntpg:"))
async def cb_hunt_page(cb: CallbackQuery) -> None:
    parts = cb.data.split(":")
    if len(parts) < 5:
        await cb.answer("Битый callback", show_alert=True)
        return
    _, rid_s, mp_short, ch, page_s = parts
    rid = int(rid_s)
    mp = "wb" if mp_short == "w" else "ozon"
    cluster = _cluster_by_hash(rid, mp, ch)
    if not cluster:
        await cb.answer("Кэш истёк — запусти «🔍 Подобрать склад WB» заново", show_alert=True)
        return
    await cb.answer()
    if cb.message:
        # Редактируем то же сообщение, чтобы не плодить новые
        await _render_hunt_page(cb.message, rid, mp, cluster, page=int(page_s), edit=True)


@router.callback_query(F.data.startswith("skip_dir:"))
async def cb_skip_direction(cb: CallbackQuery) -> None:
    parts = cb.data.split(":", 1)
    rid = int(parts[1]) if len(parts) == 2 else None
    await cb.answer("Пропущено")
    if cb.message and rid:
        # Возвращаемся в карточку заявки
        tg_id_skip = cb.from_user.id if cb.from_user else 0
        with db_session() as session:
            req = get_shipment_request(session, rid, user_id=tg_id_skip)
            if req:
                text, kb = _render_request_card(req)
                await safe_edit_or_answer(cb.message, text, reply_markup=kb)


# ── /ship_tz — генератор ТЗ Отгрузка xlsx ───────────────────────────────────

@router.message(Command("ship_tz"))
async def cmd_ship_tz(msg: Message, command: CommandObject) -> None:
    try:
        rid = int((command.args or "").strip())
    except ValueError:
        await msg.answer("Использование: <code>/ship_tz ID</code>")
        return
    await _send_ship_tz(msg, rid)


@router.callback_query(F.data.startswith("ship_items:"))
async def cb_ship_items(cb: CallbackQuery) -> None:
    """Развёрнутый состав заявки по кластерам с маркировкой забронированных."""
    rid = int(cb.data.split(":", 1)[1])
    await cb.answer()
    if not cb.message:
        return

    tg_id_items = cb.from_user.id if cb.from_user else 0
    with db_session() as session:
        req = get_shipment_request(session, rid, user_id=tg_id_items)
        if not req:
            await cb.message.answer(f"Поставка #{rid} не найдена.")
            return
        # Группируем: (marketplace, cluster) → [(article, qty, booked_supply_id, target_warehouse)]
        groups: Dict[Tuple[str, str], List[Tuple]] = {}
        for it in req.items:
            key = (it.marketplace, it.cluster)
            # Берём «канонический» артикул из маркет-каталога если есть
            mp = (it.marketplace or "").lower()
            canon = None
            if mp == "ozon" and it.ozon_product:
                canon = it.ozon_product.offer_id
            elif mp == "wb" and it.wb_product:
                canon = it.wb_product.article or str(it.wb_product.nm_id)
            groups.setdefault(key, []).append((
                it.raw_article,
                canon,
                it.qty,
                it.booked_supply_id,
                it.target_warehouse,
            ))

    if not groups:
        await cb.message.answer(f"Заявка #{rid} пуста.")
        return

    lines = [f"🛒 <b>Состав заявки #{rid}</b>\n"]
    mp_emoji = {"wb": "🟣", "ozon": "🔵"}
    keys_sorted = sorted(groups.keys(), key=lambda x: (x[0] != "ozon", x[1]))
    for mp, cl in keys_sorted:
        items = groups[(mp, cl)]
        total = sum(qty for _, _, qty, _, _ in items)
        booked_count = sum(1 for _, _, _, bsid, _ in items if bsid)
        emoji = mp_emoji.get(mp, "•")
        booked_mark = ""
        if booked_count and booked_count == len(items):
            booked_mark = " ✅ Забронировано"
        elif booked_count:
            booked_mark = f" ⚠ Частично забронировано ({booked_count}/{len(items)})"
        lines.append(f"{emoji} <b>{cl}</b> ({len(items)} SKU, {total} шт){booked_mark}")
        for raw, art, qty, bsid, twh in items:
            label = art or raw
            book_info = ""
            if bsid:
                book_info = f"  ✓ → {twh or '?'} · #{bsid}"
            lines.append(f"   • <code>{label}</code> × {qty}{book_info}")
        lines.append("")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀ К карточке", callback_data=f"ship_open:{rid}")],
    ])
    text = "\n".join(lines)
    # Telegram лимит на edit_text = 4096. Если длиннее — режем превью.
    if len(text) > 3900:
        text = text[:3850] + "\n\n<i>…длинный список, обрезано</i>"
    await safe_edit_or_answer(cb.message, text, reply_markup=kb)


@router.callback_query(F.data.startswith("ship_tz:"))
async def cb_ship_tz(cb: CallbackQuery) -> None:
    rid = int(cb.data.split(":", 1)[1])
    await cb.answer("Генерирую ТЗ Отгрузка…")
    if cb.message:
        await _send_ship_tz(cb.message, rid)


async def _send_ship_tz(msg: Message, rid: int) -> None:
    from aiogram.types import BufferedInputFile
    from src.generators import generate_ship_tz

    # Перед генерацией подтягиваем актуальные ozon_order_number — иначе если
    # юзер сразу после bulk-book жмёт «ТЗ», под шапками будут числовые order_id
    # вместо красивого номера ЛК. Не блокируем на ошибках.
    try:
        from src.services.ozon_supply_status_service import refresh_supply_status, is_cache_fresh
        if not is_cache_fresh(rid):
            tg_id = current_user_id_from(msg)
            cli = None
            if tg_id is not None:
                with db_session() as session:
                    cli = get_ozon_client_for(session, tg_id)
            if cli is not None:
                with db_session() as session:
                    await refresh_supply_status(session, cli, rid, force=False)
    except Exception as e:
        logger.warning("ship_tz: status refresh failed rid=%s: %s", rid, e)

    tg_id_tz = msg.from_user.id if msg.from_user else 0
    with db_session() as session:
        req = get_shipment_request(session, rid, user_id=tg_id_tz)
        if not req:
            await msg.answer(f"Поставка #{rid} не найдена.")
            return
        try:
            data = generate_ship_tz(req)
        except Exception as e:
            await msg.answer(f"⚠ Ошибка генерации: <code>{type(e).__name__}: {e}</code>")
            return
        n_items = len(req.items)
        clusters_wb = sorted({i.cluster for i in req.items if i.marketplace == "wb"})
        clusters_oz = sorted({i.cluster for i in req.items if i.marketplace == "ozon"})

    fname = f"TZ_Otgruzka_request_{rid}.xlsx"
    caption = (
        f"📤 ТЗ Отгрузка для заявки #{rid}\n"
        f"Строк: {n_items}\n"
    )
    if clusters_wb:
        caption += f"WB кластеры: {', '.join(clusters_wb)}\n"
    if clusters_oz:
        caption += f"Ozon кластеры: {', '.join(clusters_oz)}\n"
    caption += "\nКолонки supply_id и дата заполнятся после бронирования."
    await msg.answer_document(
        document=BufferedInputFile(data, filename=fname),
        caption=caption,
    )
