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
from typing import Dict, List, Optional

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

router = Router()
logger = logging.getLogger("bot.shipment")


class ShipPick(StatesGroup):
    pick_request = State()


class ShipPlan(StatesGroup):
    dates = State()
    crossdock_mode = State()         # один склад для всех или индивидуально
    crossdock_each_pick = State()    # выбор направления (если индивидуально)
    crossdock_each_set = State()     # выбор склада для конкретного направления
    confirm = State()


# Кросс-док только для Ozon (через WB API поставку всё равно не создать)
CROSSDOCK_OPTIONS = [
    "Прямая поставка (без кросс-дока)",
    "ЛЕБЕР Домодедово → Внуково (Ozon)",
    "ЛЕБЕР Домодедово → Хоругвино (Ozon)",
    "Любой подходящий",
]

# Кэш результатов ханта: (rid, mp, wid) → {name, dlv_coef, coef, dates: [iso]}.
# Используется для двухступенчатого выбора: склад → дата. Volatile (в памяти).
_HUNT_CACHE: Dict[tuple, dict] = {}


# ── /ship ───────────────────────────────────────────────────────────────────

@router.message(Command("ship"))
async def cmd_ship(msg: Message) -> None:
    await _render_ship_list(msg, edit=False)


async def _render_ship_list(target: Message, *, edit: bool = False) -> None:
    """Список заявок с инлайн-кнопками. Группировка: WB / Ozon / Смешанные.
    edit=True пытается отредактировать сообщение target вместо answer."""
    wb_rows: List[InlineKeyboardButton] = []
    oz_rows: List[InlineKeyboardButton] = []
    mix_rows: List[InlineKeyboardButton] = []
    total = 0
    with db_session() as session:
        reqs = list_shipment_requests(session, limit=30)
        for r in reqs:
            if r.state not in {"draft", "planning", "slot_searching"}:
                continue
            total += 1
            mps = {it.marketplace for it in r.items}
            n_items = len(r.items)
            date_s = r.created_at.strftime("%d.%m")
            if mps == {"wb"}:
                emoji = "🟣"; bucket = wb_rows
            elif mps == {"ozon"}:
                emoji = "🔵"; bucket = oz_rows
            else:
                emoji = "🟡"; bucket = mix_rows
            label = f"{emoji} #{r.id} [{r.state}] · {n_items} строк · {date_s}"
            bucket.append(InlineKeyboardButton(text=label[:55], callback_data=f"ship_open:{r.id}"))

    if not total:
        text = "🚚 <b>Заявки на отгрузку</b>\n\nОткрытых заявок нет.\n\n📎 Кинь .xlsx-выгрузку чтобы создать."
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:home")],
        ])
    else:
        lines = ["🚚 <b>Заявки на отгрузку</b>\n"]
        rows: List[List[InlineKeyboardButton]] = []
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
    with db_session() as session:
        req = get_shipment_request(session, rid)
        if not req:
            await msg.answer(f"Заявка #{rid} не найдена.")
            return
        text, kb = _render_request_card(req)
    await msg.answer(text, reply_markup=kb)


def _render_request_card(req) -> tuple:
    """Внутри сессии собрать текст карточки + клавиатуру."""
    summary = shipment_summary(req)
    files = req.source_files_json or []
    crossdock = req.crossdock_warehouses_json or {}
    has_ozon = any(it.marketplace == "ozon" for it in req.items)
    has_wb = any(it.marketplace == "wb" for it in req.items)

    text = (
        f"📦 <b>Заявка #{req.id}</b> [{req.state}]\n"
        f"Создана: {req.created_at:%Y-%m-%d %H:%M}\n"
        f"Файлов: {len(files)}\n\n"
        f"<b>Распределение:</b>\n{summary}"
    )

    if req.target_date_from:
        date_s = f"{req.target_date_from:%Y-%m-%d}"
        if req.target_date_to:
            date_s += f" — {req.target_date_to:%Y-%m-%d}"
        text += f"\n\n<b>Целевые даты:</b> {date_s}"

    if crossdock:
        text += "\n\n<b>Кросс-док:</b>"
        for k, v in crossdock.items():
            text += f"\n  {k}: {v}"

    if files:
        text += "\n\n<b>Источники:</b>\n" + "\n".join(f"  • {f}" for f in files[-3:])

    rows = []
    # Этап 1: даты ещё не выбраны
    if req.state == "draft":
        rows.append([InlineKeyboardButton(text="🛠 Спланировать даты",
                                         callback_data=f"ship_plan:{req.id}")])
    else:
        # Этап 2: даты есть → разные кнопки в зависимости от MP
        if has_wb:
            rows.append([InlineKeyboardButton(text="🔍 Подобрать склад WB",
                                             callback_data=f"ship_hunt:{req.id}")])
        if has_ozon:
            rows.append([
                InlineKeyboardButton(text="🚀 Ozon → DIRECT (РФЦ)",
                                     callback_data=f"ozon_book_card:{req.id}:direct"),
            ])
            rows.append([
                InlineKeyboardButton(text="🚛 Ozon → CROSSDOCK (хаб)",
                                     callback_data=f"ozon_book_card:{req.id}:cross"),
            ])
        rows.append([InlineKeyboardButton(text="🛠 Изменить даты",
                                         callback_data=f"ship_plan:{req.id}")])
    if has_wb:
        rows.append([InlineKeyboardButton(text="🌐 WB ЛК → Поставки",
                                         url="https://seller.wildberries.ru/supplies-management/all-supplies")])
    if has_ozon:
        rows.append([InlineKeyboardButton(text="🌐 Ozon ЛК → Поставки",
                                         url="https://seller.ozon.ru/app/supply-orders")])
    rows.append([
        InlineKeyboardButton(text="📤 ТЗ xlsx", callback_data=f"ship_tz:{req.id}"),
        InlineKeyboardButton(text="📎 + Файл", callback_data="ship_more"),
    ])
    rows.append([
        InlineKeyboardButton(text="◀ К списку", callback_data="menu:ships"),
        InlineKeyboardButton(text="🗑 Удалить", callback_data=f"ship_del:{req.id}"),
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
    with db_session() as session:
        reqs = list_shipment_requests(session, limit=10)
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
        # Создаём сразу новую заявку
        with db_session() as session:
            req = create_shipment_request(session, source_file=fname)
            result = attach_ship_file(session, req.id, parsed)
            rid = req.id
        await _send_attach_result(msg, rid, result)
        return

    # Спросим: новую или к существующей?
    rows = [[InlineKeyboardButton(text="➕ Новая заявка", callback_data="ship_new")]]
    for rid, rstate, n_items in open_summaries:
        rows.append([InlineKeyboardButton(
            text=f"➤ #{rid} ({n_items} строк, {rstate})",
            callback_data=f"ship_attach:{rid}",
        )])
    rows.append([InlineKeyboardButton(text="✖ Отмена", callback_data="cancel")])
    await state.set_state(ShipPick.pick_request)
    await msg.answer(
        "К какой заявке привязать?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(ShipPick.pick_request, F.data == "ship_new")
async def cb_ship_new(cb: CallbackQuery, state: FSMContext) -> None:
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
        req = create_shipment_request(session, source_file=fname)
        result = attach_ship_file(session, req.id, parsed)
        rid = req.id

    await cb.answer("Создано")
    if cb.message:
        await safe_edit_or_answer(cb.message, f"✅ Создана новая заявка #{rid}")
        await _send_attach_result(cb.message, rid, result)


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
        await safe_edit_or_answer(cb.message, f"✅ Привязано к заявке #{rid}")
        await _send_attach_result(cb.message, rid, result)


async def _send_attach_result(msg_or_cb_msg: Message, rid: int, result) -> None:
    lines = [
        f"📦 Заявка #{rid} — добавлено <b>{result.items_added}</b> строк "
        f"({result.cluster} / {result.marketplace.upper()})",
        f"✅ В каталоге: {result.matched}",
    ]
    if result.unmatched_articles:
        lines.append(f"⚠ Не нашёл в каталоге: {len(result.unmatched_articles)}")
        for a in result.unmatched_articles[:15]:
            lines.append(f"  <code>{a}</code>")
        if len(result.unmatched_articles) > 15:
            lines.append(f"  …ещё {len(result.unmatched_articles) - 15}")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Открыть заявку", callback_data=f"ship_open:{rid}")],
        [InlineKeyboardButton(text="📎 Привязать ещё файл", callback_data="ship_more")],
    ])
    await send_long(msg_or_cb_msg, "\n".join(lines), reply_markup=kb)


@router.callback_query(F.data.startswith("ship_open:"))
async def cb_ship_open(cb: CallbackQuery) -> None:
    rid = int(cb.data.split(":", 1)[1])
    with db_session() as session:
        req = get_shipment_request(session, rid)
        if not req:
            await cb.answer("Не найдена", show_alert=True)
            return
        text, kb = _render_request_card(req)
    await cb.answer()
    if cb.message:
        await safe_edit_or_answer(cb.message, text, reply_markup=kb)


@router.callback_query(F.data.startswith("ship_del:"))
async def cb_ship_del(cb: CallbackQuery) -> None:
    rid = int(cb.data.split(":", 1)[1])
    with db_session() as session:
        req = get_shipment_request(session, rid)
        if not req:
            await cb.answer("Не найдена", show_alert=True)
            return
        session.delete(req)
    await cb.answer("Удалено")
    if cb.message:
        # Возвращаемся к списку заявок
        await _render_ship_list(cb.message, edit=True)


@router.callback_query(F.data == "ship_more")
async def cb_ship_more(cb: CallbackQuery) -> None:
    await cb.answer("Жду файл…")
    if cb.message:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀ К списку заявок", callback_data="menu:ships")],
        ])
        await safe_edit_or_answer(
            cb.message,
            "📎 Кинь следующий xlsx-файл выгрузки — добавлю в текущую заявку "
            "или создам новую.",
            reply_markup=kb,
        )


# ── /ship_plan — этап 2: даты + кросс-док ────────────────────────────────────

from src.bot.keyboards import kb_dates_picker  # переиспользуем календарь


@router.message(Command("ship_plan"))
async def cmd_ship_plan(msg: Message, command: CommandObject, state: FSMContext) -> None:
    try:
        rid = int((command.args or "").strip())
    except ValueError:
        await msg.answer("Использование: <code>/ship_plan ID</code>")
        return
    await _start_plan_wizard(msg, state, rid)


@router.callback_query(F.data.startswith("ship_plan:"))
async def cb_ship_plan(cb: CallbackQuery, state: FSMContext) -> None:
    rid = int(cb.data.split(":", 1)[1])
    await cb.answer()
    if cb.message:
        await _start_plan_wizard(cb.message, state, rid, edit=True)


async def _start_plan_wizard(
    msg: Message, state: FSMContext, rid: int, *, edit: bool = False,
) -> None:
    """Показать заявку + календарь дат. edit=True редактирует исходное сообщение.

    Если у заявки уже есть target_date_from/to — предзаполняет выбранные галочки
    в календаре (пользователь видит свой прошлый выбор и может его подкорректировать).
    """
    from datetime import date as _date
    with db_session() as session:
        req = get_shipment_request(session, rid)
        if not req:
            await msg.answer(f"Заявка #{rid} не найдена.")
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
        f"🛠 <b>Планирование заявки #{rid}</b> ({n_items} строк, {len(directions)} направлений)\n\n"
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


@router.callback_query(ShipPlan.dates, F.data == "dp_cl")
async def cb_sp_clear(cb: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(ship_plan_selected_offsets=[])
    await cb.answer("Сброшено")
    if cb.message:
        try:
            await cb.message.edit_reply_markup(
                reply_markup=kb_dates_picker(set(), days_ahead=14, min_offset=0)
            )
        except Exception:
            pass


@router.callback_query(ShipPlan.dates, F.data == "dp_skip")
async def cb_sp_skip(cb: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(ship_plan_target_date_from=None, ship_plan_target_date_to=None)
    await cb.answer("Без целевых дат")
    if cb.message:
        await _ask_crossdock_mode(cb.message, state)


@router.callback_query(ShipPlan.dates, F.data == "dp_man")
async def cb_sp_manual(cb: CallbackQuery) -> None:
    await cb.answer("Сейчас доступен только выбор тапом. Без даты — кнопка ⏭", show_alert=True)


@router.callback_query(ShipPlan.dates, F.data == "dp_ok")
async def cb_sp_confirm_dates(cb: CallbackQuery, state: FSMContext) -> None:
    from datetime import date as _date, timedelta
    data = await state.get_data()
    selected = sorted(data.get("ship_plan_selected_offsets", []))
    if not selected:
        await cb.answer("Выбери хотя бы одну дату или нажми ⏭", show_alert=True)
        return
    today = _date.today()
    dates = [today + timedelta(days=n) for n in selected]
    d_from = min(dates)
    d_to = max(dates) if len(dates) > 1 else None
    await state.update_data(
        ship_plan_target_date_from=d_from.isoformat(),
        ship_plan_target_date_to=d_to.isoformat() if d_to else None,
    )
    label = f"{d_from:%Y-%m-%d}"
    if d_to:
        label += f" — {d_to:%Y-%m-%d}"
    await cb.answer(f"Даты: {label}")
    if cb.message:
        # Сразу к подтверждению — _show_confirm отредактирует это же сообщение
        await _ask_crossdock_mode(cb.message, state)


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
    crossdock = data.get("ship_plan_crossdock", {})
    await state.clear()

    with db_session() as session:
        req = get_shipment_request(session, rid)
        if not req:
            await cb.answer("Не найдена", show_alert=True)
            return
        if d_from:
            req.target_date_from = _dt.fromisoformat(d_from)
        if d_to:
            req.target_date_to = _dt.fromisoformat(d_to)
        req.crossdock_warehouses_json = crossdock
        req.state = "planning"

    await cb.answer("✅ План сохранён")
    if cb.message:
        # Показываем обновлённую карточку с кнопками действий
        with db_session() as session:
            req = get_shipment_request(session, rid)
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
    from src.config import APIKEY_OZON, CLIENT_ID_OZON, APIKEY_WB, OZON_PROXY_URL
    from src.integrations import OzonClient, WBClient
    from src.services.slot_hunter import hunt_wb, hunt_ozon

    with db_session() as session:
        req = get_shipment_request(session, rid)
        if not req:
            await msg.answer(f"Заявка #{rid} не найдена.")
            return
        if req.state not in {"planning", "slot_searching", "draft"}:
            await msg.answer(f"Заявка #{rid} в состоянии [{req.state}] — разведка не нужна.")
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
            sku = it.sku
            if not sku or not sku.barcode:
                continue
            bucket = wb_goods_by_cluster.setdefault(it.cluster, {})
            bucket[sku.barcode] = bucket.get(sku.barcode, 0) + it.qty
        if req.state == "draft":
            req.state = "slot_searching"

    if not target_dates:
        await msg.answer("⚠ У заявки не указаны целевые даты — пройди /ship_plan сначала.")
        return

    await msg.answer(
        f"🔍 <b>Разведка слотов для #{rid}</b>\n"
        f"Дат: {len(target_dates)} ({target_dates[0]:%Y-%m-%d} — {target_dates[-1]:%Y-%m-%d})\n"
        f"Направлений: {len(directions)}"
    )

    wb_cli = WBClient(APIKEY_WB) if APIKEY_WB else None
    oz_cli = OzonClient(CLIENT_ID_OZON, APIKEY_OZON, proxy=OZON_PROXY_URL) if (APIKEY_OZON and CLIENT_ID_OZON) else None

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
            await msg.answer("⚠ APIKEY_WB не задан — пропускаю WB.")
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

    with db_session() as session:
        req = get_shipment_request(session, rid)
        if not req:
            await cb.answer("Заявка не найдена", show_alert=True)
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
                [InlineKeyboardButton(text="📋 Карточка заявки", callback_data=f"ship_open:{rid}")],
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
        with db_session() as session:
            req = get_shipment_request(session, rid)
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


@router.callback_query(F.data.startswith("ship_tz:"))
async def cb_ship_tz(cb: CallbackQuery) -> None:
    rid = int(cb.data.split(":", 1)[1])
    await cb.answer("Генерирую ТЗ Отгрузка…")
    if cb.message:
        await _send_ship_tz(cb.message, rid)


async def _send_ship_tz(msg: Message, rid: int) -> None:
    from aiogram.types import BufferedInputFile
    from src.generators import generate_ship_tz

    with db_session() as session:
        req = get_shipment_request(session, rid)
        if not req:
            await msg.answer(f"Заявка #{rid} не найдена.")
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
