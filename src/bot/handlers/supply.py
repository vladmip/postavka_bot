import re
from datetime import date, datetime, timedelta
from typing import List, Optional

from aiogram import Router, F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext

from src.bot.helpers import safe_edit_or_answer
from src.bot.keyboards import (
    kb_marketplace, kb_clusters, kb_warehouses_in_cluster,
    resolve_warehouse, resolve_ozon_cluster_warehouses,
    kb_supply_card, kb_pick_sku, kb_slot_choice, kb_dates_picker,
)
from src.bot.states import SupplyNew, SupplyAddItem
from src.db.session import db_session
from src.db.models import Supply
from src.services.catalog_service import list_skus, get_sku
from src.services.supply_service import (
    create_supply, get_supply, list_supplies, add_item, transition,
)
from src.warehouses import WB_FOOD_WAREHOUSES, wb_cluster_names, ozon_cluster_names

router = Router()

WB_MIN_LEAD_DAYS = 5

_MONTH_RU = {
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "май": 5, "мая": 5,
    "июн": 6, "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
}


# ── helpers ─────────────────────────────────────────────────────────────────

def _format_slot(supply) -> str:
    """Форматирует слот: одиночная дата, диапазон, или список через запятую."""
    dates = supply.slot_dates_json or []
    if dates and len(dates) >= 2:
        # Парсим как date-объекты, проверяем подряд ли
        try:
            ds = sorted(date.fromisoformat(d) for d in dates)
        except Exception:
            ds = []
        if ds:
            is_consecutive = all((ds[i + 1] - ds[i]).days == 1 for i in range(len(ds) - 1))
            if is_consecutive:
                return f"{ds[0]:%Y-%m-%d} — {ds[-1]:%Y-%m-%d}"
            return ", ".join(d.strftime("%Y-%m-%d") for d in ds)
    if supply.slot_at:
        s = f"{supply.slot_at:%Y-%m-%d}"
        if supply.slot_date_to:
            s += f" — {supply.slot_date_to:%Y-%m-%d}"
        return s
    return ""


def _format_supply_card(supply) -> str:
    lines = [
        f"📦 Поставка #{supply.id}",
        f"МП: {supply.marketplace.upper()}",
        f"Склад: {supply.warehouse}",
        f"Состояние: <b>{supply.state}</b>",
    ]
    slot_str = _format_slot(supply)
    if slot_str:
        lines.append(f"Слот: {slot_str}")
    if supply.comments:
        lines.append(f"Комментарий: {supply.comments}")

    if supply.items:
        lines.append("\n<b>Позиции:</b>")
        for it in supply.items:
            mark = ""
            if it.expanded_from_kit_id:
                mark = "  ↳"
            elif any(o.expanded_from_kit_id == it.sku_id for o in supply.items):
                mark = "🎁"
            picked = f" /pk:{it.qty_picked}" if it.qty_picked is not None else ""
            accepted = f" /ac:{it.qty_accepted}" if it.qty_accepted is not None else ""
            article = it.sku.article if it.sku else "?"
            lines.append(f"{mark} <code>{article}</code> × {it.qty_planned}{picked}{accepted}")
    else:
        lines.append("\n(нет позиций)")

    return "\n".join(lines)


def _parse_date_range(raw: str, year: int = None):
    """
    Парсит одиночные и диапазонные даты:
      '2026-05-20'        → (date(2026,5,20), None)
      '12-14 мая'         → (date(year,5,12), date(year,5,14))
      'с 19 до 22'        → (date(year,m,19), date(year,m,22))  -- m=текущий месяц
      'с 19 по 22 мая'    → (date(year,5,19), date(year,5,22))
    Возвращает (slot_from, slot_to) или бросает ValueError.
    """
    if year is None:
        year = date.today().year
    raw = raw.strip().lower()

    # ISO single
    if re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
        d = datetime.strptime(raw, "%Y-%m-%d").date()
        return d, None

    # ISO range: '2026-05-19 - 2026-05-21' / '2026-05-19—2026-05-21'
    m = re.match(r"^(\d{4}-\d{2}-\d{2})\s*[-–—]\s*(\d{4}-\d{2}-\d{2})$", raw)
    if m:
        d1 = datetime.strptime(m.group(1), "%Y-%m-%d").date()
        d2 = datetime.strptime(m.group(2), "%Y-%m-%d").date()
        if d2 < d1:
            d1, d2 = d2, d1
        return d1, d2

    # "12-14 мая" / "12-14 мая 2026"
    m = re.match(r"^(\d{1,2})\s*[-–]\s*(\d{1,2})\s+([а-яё]+)(?:\s+(\d{4}))?$", raw)
    if m:
        d1, d2, mon_s, yr_s = int(m.group(1)), int(m.group(2)), m.group(3)[:3], m.group(4)
        mon = _MONTH_RU.get(mon_s)
        if not mon:
            raise ValueError(f"Не распознал месяц: {mon_s!r}")
        y = int(yr_s) if yr_s else year
        return date(y, mon, d1), date(y, mon, d2)

    # "с 19 до 22" / "с 19 по 22" (текущий месяц)
    m = re.match(r"^с\s+(\d{1,2})\s+(?:до|по)\s+(\d{1,2})(?:\s+([а-яё]+))?(?:\s+(\d{4}))?$", raw)
    if m:
        d1, d2 = int(m.group(1)), int(m.group(2))
        mon_s = (m.group(3) or "")[:3]
        yr_s = m.group(4)
        mon = _MONTH_RU.get(mon_s, date.today().month)
        y = int(yr_s) if yr_s else year
        return date(y, mon, d1), date(y, mon, d2)

    raise ValueError(f"Не распознал формат даты: {raw!r}")


# ── /supply_new ──────────────────────────────────────────────────────────────

@router.message(Command("supply_new"))
async def cmd_supply_new(msg: Message, state: FSMContext) -> None:
    await state.set_state(SupplyNew.marketplace)
    await msg.answer("🏪 Шаг 1/4. Выбери маркетплейс:", reply_markup=kb_marketplace())


@router.callback_query(SupplyNew.marketplace, F.data.startswith("mp:"))
async def supply_new_mp(cb: CallbackQuery, state: FSMContext) -> None:
    mp = cb.data.split(":", 1)[1]
    await state.update_data(marketplace=mp)
    await state.set_state(SupplyNew.cluster)

    warning = ""
    if mp == "ozon":
        warning = "\n⚠ Паллетная поставка на Озон может стоить дорого. Проверь тариф в ЛК до бронирования."
    if mp == "wb":
        warning = f"\n⚠ Слот WB бронируй минимум на +{WB_MIN_LEAD_DAYS} дней — иначе штраф за перенос."

    await cb.answer()
    if cb.message:
        await safe_edit_or_answer(
            cb.message,
            f"🏪 МП: <b>{mp.upper()}</b>{warning}\n\n📦 Шаг 2/4. Выбери кластер склада:",
            reply_markup=kb_clusters(mp),
        )


@router.callback_query(SupplyNew.cluster, F.data.startswith("cl_wb:") | F.data.startswith("cl_oz:"))
async def supply_new_cluster(cb: CallbackQuery, state: FSMContext) -> None:
    prefix, idx_s = cb.data.split(":", 1)
    mp = "wb" if prefix == "cl_wb" else "ozon"
    cluster_idx = int(idx_s)

    cluster_names = wb_cluster_names() if mp == "wb" else ozon_cluster_names()
    cluster_name = cluster_names[cluster_idx]

    await state.update_data(cluster_idx=cluster_idx)
    await state.set_state(SupplyNew.warehouse)
    await cb.answer()
    if cb.message:
        await safe_edit_or_answer(
            cb.message,
            f"📦 Кластер: <b>{cluster_name}</b>\n\nШаг 3/4. Выбери склад:",
            reply_markup=kb_warehouses_in_cluster(mp, cluster_idx),
        )


@router.callback_query(SupplyNew.cluster, F.data == "wh_custom")
async def supply_new_cluster_custom(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SupplyNew.warehouse_custom)
    await cb.answer()
    if cb.message:
        await cb.message.edit_text("✍ Напиши название склада сообщением:")


@router.callback_query(SupplyNew.warehouse, F.data == "wh_custom")
async def supply_new_wh_custom_btn(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SupplyNew.warehouse_custom)
    await cb.answer()
    if cb.message:
        await cb.message.edit_text("✍ Напиши название склада сообщением:")


@router.callback_query(SupplyNew.warehouse, F.data.startswith("cl_back:"))
async def supply_new_back_to_clusters(cb: CallbackQuery, state: FSMContext) -> None:
    mp = cb.data.split(":", 1)[1]
    await state.set_state(SupplyNew.cluster)
    await cb.answer()
    if cb.message:
        await safe_edit_or_answer(
            cb.message,
            f"📦 Шаг 2/4. Выбери кластер склада ({mp.upper()}):",
            reply_markup=kb_clusters(mp),
        )


@router.callback_query(SupplyNew.warehouse, F.data.startswith("wh_wb:") | F.data.startswith("wh_oz:"))
async def supply_new_wh(cb: CallbackQuery, state: FSMContext) -> None:
    prefix, payload = cb.data.split(":", 1)
    mp = "wb" if prefix == "wh_wb" else "ozon"

    wh = resolve_warehouse(mp, payload)
    if not wh:
        await cb.answer(f"Не нашёл склад по индексу {payload}", show_alert=True)
        return

    extra = ""
    if mp == "wb" and wh not in WB_FOOD_WAREHOUSES:
        extra = "\n⚠ Этот склад не в списке «питание» — обычно не принимает продукты."

    await state.update_data(warehouse=wh)
    await state.set_state(SupplyNew.slot_choice)
    await cb.answer()
    if cb.message:
        await safe_edit_or_answer(
            cb.message,
            f"📦 Склад: <b>{wh}</b>{extra}\n\n📅 Шаг 4/4. Указать дату слота?",
            reply_markup=kb_slot_choice(mp),
        )


@router.callback_query(SupplyNew.warehouse, F.data.startswith("wh_oz_all:"))
async def supply_new_wh_oz_all(cb: CallbackQuery, state: FSMContext) -> None:
    """Создать поставки для всех складов в Ozon-кластере."""
    cluster_idx = int(cb.data.split(":", 1)[1])
    warehouses = resolve_ozon_cluster_warehouses(cluster_idx)
    if not warehouses:
        await cb.answer("Нет складов в кластере", show_alert=True)
        return

    await state.update_data(cluster_warehouses=warehouses, warehouse=None)
    await state.set_state(SupplyNew.slot_choice)
    await cb.answer()
    if cb.message:
        wh_list = "\n".join(f"  • {w}" for w in warehouses)
        await safe_edit_or_answer(
            cb.message,
            f"📦 Будут созданы поставки на все склады кластера:\n{wh_list}\n\n"
            f"📅 Шаг 4/4. Указать дату слота?",
            reply_markup=kb_slot_choice("ozon"),
        )


@router.message(SupplyNew.warehouse_custom, F.text)
async def supply_new_wh_custom(msg: Message, state: FSMContext) -> None:
    wh = (msg.text or "").strip()
    if not wh:
        return
    await state.update_data(warehouse=wh)
    await state.set_state(SupplyNew.slot_choice)
    data = await state.get_data()
    mp = data.get("marketplace", "")
    await msg.answer(
        f"📦 Склад: <b>{wh}</b>\n\n📅 Шаг 4/4. Указать дату слота?",
        reply_markup=kb_slot_choice(mp),
    )


@router.callback_query(SupplyNew.slot_choice, F.data.startswith("slot:"))
async def supply_new_slot_choice(cb: CallbackQuery, state: FSMContext) -> None:
    action = cb.data.split(":", 1)[1]
    if action == "skip":
        await _finalize_supply(cb, state, slot_at=None, slot_date_to=None, slot_dates=None)
        return

    data = await state.get_data()
    mp = data.get("marketplace", "")

    if action == "cal":
        await state.set_state(SupplyNew.slot_date)
        await state.update_data(selected_offsets=[])
        min_offset = WB_MIN_LEAD_DAYS if mp == "wb" else 0
        await state.update_data(min_offset=min_offset)
        hint = ""
        if mp == "wb":
            hint = f"\n🚫 — даты раньше +{WB_MIN_LEAD_DAYS} дней закрыты (штраф WB)."
        if mp == "ozon":
            hint = "\nМожно выбрать одну дату или несколько (в diap. min..max)."
        await cb.answer()
        if cb.message:
            await safe_edit_or_answer(
                cb.message,
                f"📅 Выбери дату/диапазон тапом:{hint}",
                reply_markup=kb_dates_picker(set(), min_offset=min_offset),
            )
        return

    # action == "manual"
    await state.set_state(SupplyNew.slot_date)
    hint = ""
    if mp == "wb":
        min_d = (date.today() + timedelta(days=WB_MIN_LEAD_DAYS)).strftime("%Y-%m-%d")
        hint = f"\nДля WB минимум — {min_d} (иначе штраф)."
    ozon_hint = ""
    if mp == "ozon":
        ozon_hint = "\nДиапазон: <code>2026-05-19 - 2026-05-21</code>, <code>12-14 мая</code> или <code>с 19 по 22</code>."

    await cb.answer()
    if cb.message:
        await safe_edit_or_answer(
            cb.message,
            f"📅 Введи дату в формате <code>YYYY-MM-DD</code>.{hint}{ozon_hint}"
        )


# ── календарь: tap-выбор дат ─────────────────────────────────────────────────

@router.callback_query(SupplyNew.slot_date, F.data == "dp_lock")
async def cb_dp_lock(cb: CallbackQuery) -> None:
    await cb.answer(f"Эта дата раньше +{WB_MIN_LEAD_DAYS} дней — штраф WB", show_alert=True)


@router.callback_query(SupplyNew.slot_date, F.data.startswith("dp:"))
async def cb_dp_toggle(cb: CallbackQuery, state: FSMContext) -> None:
    n = int(cb.data.split(":", 1)[1])
    data = await state.get_data()
    selected = set(data.get("selected_offsets", []))
    min_offset = data.get("min_offset", 0)

    if n in selected:
        selected.remove(n)
    else:
        selected.add(n)

    await state.update_data(selected_offsets=sorted(selected))
    await cb.answer()
    if cb.message:
        try:
            await cb.message.edit_reply_markup(
                reply_markup=kb_dates_picker(selected, min_offset=min_offset)
            )
        except Exception:
            pass


@router.callback_query(SupplyNew.slot_date, F.data == "dp_cl")
async def cb_dp_clear(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    min_offset = data.get("min_offset", 0)
    await state.update_data(selected_offsets=[])
    await cb.answer("Сброшено")
    if cb.message:
        try:
            await cb.message.edit_reply_markup(
                reply_markup=kb_dates_picker(set(), min_offset=min_offset)
            )
        except Exception:
            pass


@router.callback_query(SupplyNew.slot_date, F.data == "dp_skip")
async def cb_dp_skip(cb: CallbackQuery, state: FSMContext) -> None:
    await _finalize_supply(cb, state, slot_at=None, slot_date_to=None, slot_dates=None)


@router.callback_query(SupplyNew.slot_date, F.data == "dp_man")
async def cb_dp_manual(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    mp = data.get("marketplace", "")
    hint = ""
    if mp == "ozon":
        hint = "\nДиапазон: <code>2026-05-19 - 2026-05-21</code> или <code>12-14 мая</code>."
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    back_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="◀ Назад в календарь", callback_data="dp_back_cal"),
        InlineKeyboardButton(text="✖ Отмена", callback_data="cancel"),
    ]])
    await cb.answer()
    if cb.message:
        await safe_edit_or_answer(
            cb.message,
            f"📅 Введи дату в формате <code>YYYY-MM-DD</code>.{hint}",
            reply_markup=back_kb,
        )


@router.callback_query(SupplyNew.slot_date, F.data == "dp_back_cal")
async def cb_dp_back_to_calendar(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    selected = set(data.get("selected_offsets", []))
    min_offset = data.get("min_offset", 0)
    mp = data.get("marketplace", "")
    hint = ""
    if mp == "wb":
        hint = f"\n🚫 — даты раньше +{WB_MIN_LEAD_DAYS} дней закрыты (штраф WB)."
    if mp == "ozon":
        hint = "\nМожно выбрать одну дату или несколько (диапазон min..max)."
    await cb.answer()
    if cb.message:
        await safe_edit_or_answer(
            cb.message,
            f"📅 Выбери дату/диапазон тапом:{hint}",
            reply_markup=kb_dates_picker(selected, min_offset=min_offset),
        )


@router.callback_query(SupplyNew.slot_date, F.data == "dp_ok")
async def cb_dp_confirm(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    selected = sorted(data.get("selected_offsets", []))
    if not selected:
        await cb.answer("Выбери хотя бы одну дату", show_alert=True)
        return

    today = date.today()
    dates = [today + timedelta(days=n) for n in selected]
    slot_from = min(dates)
    slot_to = max(dates) if len(dates) > 1 else None

    slot_at = datetime.combine(slot_from, datetime.min.time())
    slot_date_to = datetime.combine(slot_to, datetime.min.time()) if slot_to else None
    slot_dates = [d.isoformat() for d in dates] if len(dates) >= 2 else None

    is_consecutive = len(dates) >= 2 and all((dates[i + 1] - dates[i]).days == 1 for i in range(len(dates) - 1))
    if len(dates) == 1:
        slot_str = slot_from.strftime("%Y-%m-%d")
    elif is_consecutive:
        slot_str = f"{slot_from:%Y-%m-%d} — {slot_to:%Y-%m-%d}"
    else:
        slot_str = ", ".join(d.strftime("%Y-%m-%d") for d in dates)

    await cb.answer(f"Слот: {slot_str[:50]}")
    if cb.message:
        await safe_edit_or_answer(cb.message, f"📅 Слот выбран: {slot_str}")
    await _finalize_supply(
        cb, state,
        slot_at=slot_at, slot_date_to=slot_date_to, slot_dates=slot_dates,
    )


@router.message(SupplyNew.slot_date, F.text)
async def supply_new_slot_date(msg: Message, state: FSMContext) -> None:
    raw = (msg.text or "").strip()
    data = await state.get_data()
    mp = data.get("marketplace", "")

    try:
        slot_from, slot_to = _parse_date_range(raw)
    except ValueError as e:
        await msg.answer(f"Не распознал дату: {e}\nФорматы: <code>2026-05-20</code>, <code>12-14 мая</code>, <code>с 19 по 22</code>")
        return

    today = date.today()
    if slot_from < today:
        await msg.answer(f"⚠ Дата {slot_from:%Y-%m-%d} в прошлом — не подходит. Введи ≥ сегодня.")
        return
    if slot_to and slot_to < today:
        await msg.answer(f"⚠ Конец диапазона {slot_to:%Y-%m-%d} в прошлом — не подходит.")
        return

    warning = ""
    if mp == "wb":
        days_ahead = (slot_from - today).days
        if days_ahead < WB_MIN_LEAD_DAYS:
            warning = (
                f"\n⚠ Слот через {days_ahead} дн. — это меньше +{WB_MIN_LEAD_DAYS}. "
                "Будь готов к штрафу за перенос если не успеешь собрать."
            )

    slot_str = slot_from.strftime("%Y-%m-%d")
    if slot_to:
        slot_str += f" — {slot_to:%Y-%m-%d}"
    await msg.answer(f"📅 Слот: {slot_str}{warning}")

    slot_at = datetime.combine(slot_from, datetime.min.time())
    slot_date_to = datetime.combine(slot_to, datetime.min.time()) if slot_to else None
    await _finalize_supply(msg, state, slot_at=slot_at, slot_date_to=slot_date_to, slot_dates=None)


async def _finalize_supply(
    target,
    state: FSMContext,
    slot_at: Optional[datetime],
    slot_date_to: Optional[datetime],
    slot_dates: Optional[List[str]] = None,
) -> None:
    data = await state.get_data()
    mp = data["marketplace"]
    warehouse_single: Optional[str] = data.get("warehouse")
    cluster_warehouses: Optional[List[str]] = data.get("cluster_warehouses")
    await state.clear()

    is_callback = isinstance(target, CallbackQuery)
    msg_for_reply = target.message if is_callback else target

    warehouses = cluster_warehouses if cluster_warehouses else ([warehouse_single] if warehouse_single else [])
    if not warehouses:
        if msg_for_reply:
            await msg_for_reply.answer("⚠ Не выбран склад. Начни заново: /supply_new")
        return

    if msg_for_reply:
        await msg_for_reply.answer("🤖 Создаю поставку…")

    created_ids = []
    for wh in warehouses:
        try:
            with db_session() as session:
                supply = create_supply(session, marketplace=mp, warehouse=wh)
                if slot_at is not None:
                    supply.slot_at = slot_at
                if slot_date_to is not None:
                    supply.slot_date_to = slot_date_to
                if slot_dates:
                    supply.slot_dates_json = slot_dates
                session.flush()
                created_ids.append(supply.id)
        except Exception as e:
            if is_callback:
                await target.answer(f"Ошибка: {e}", show_alert=True)
            if msg_for_reply:
                await msg_for_reply.answer(f"⚠ Ошибка создания поставки ({wh}): <code>{e}</code>")
            return

    if is_callback:
        await target.answer("Создано")

    if msg_for_reply:
        slot_str = ""
        if slot_dates and len(slot_dates) >= 2:
            try:
                ds = sorted(date.fromisoformat(d) for d in slot_dates)
                consec = all((ds[i + 1] - ds[i]).days == 1 for i in range(len(ds) - 1))
                if consec:
                    slot_str = f"\nСлот: {ds[0]:%Y-%m-%d} — {ds[-1]:%Y-%m-%d}"
                else:
                    slot_str = "\nСлот: " + ", ".join(d.strftime("%Y-%m-%d") for d in ds)
            except Exception:
                slot_str = "\nСлот: " + ", ".join(slot_dates)
        elif slot_at:
            slot_str = f"\nСлот: {slot_at:%Y-%m-%d}"
            if slot_date_to:
                slot_str += f" — {slot_date_to:%Y-%m-%d}"

        disclaimer = (
            "\n\n📌 Это <b>локальная запись</b> для планирования и генерации ТЗ.\n"
            f"В ЛК {mp.upper()} поставка ещё <b>не создана</b> — слот бронируй вручную."
        )

        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        if len(created_ids) == 1:
            sid = created_ids[0]
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="➕ Добавить SKU", callback_data=f"si_add:{sid}"),
                    InlineKeyboardButton(text="📋 Карточка", callback_data=f"sup_open:{sid}"),
                ],
                [
                    InlineKeyboardButton(text="📥 ТЗ Приёмка", callback_data=f"exp_in:{sid}"),
                    InlineKeyboardButton(text="📤 ТЗ Отгрузка", callback_data=f"exp_out:{sid}"),
                ],
            ])
            await msg_for_reply.answer(
                f"✅ Поставка <b>#{sid}</b> создана\n"
                f"МП: {mp.upper()}\nСклад: {warehouses[0]}{slot_str}{disclaimer}",
                reply_markup=kb,
            )
        else:
            kb_rows = []
            for sid, wh in zip(created_ids, warehouses):
                kb_rows.append([
                    InlineKeyboardButton(text=f"📋 #{sid} {wh[:25]}", callback_data=f"sup_open:{sid}"),
                    InlineKeyboardButton(text="➕ SKU", callback_data=f"si_add:{sid}"),
                ])
            kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
            await msg_for_reply.answer(
                f"✅ Создано {len(created_ids)} поставки (все склады кластера)\n"
                f"МП: {mp.upper()}{slot_str}{disclaimer}",
                reply_markup=kb,
            )


# ── /supply_list ─────────────────────────────────────────────────────────────

@router.message(Command("supply_list"))
async def cmd_supply_list(msg: Message, command: CommandObject) -> None:
    state_filter = (command.args or "").strip() or None
    with db_session() as session:
        supplies = list_supplies(session, state=state_filter, limit=30)
        if not supplies:
            await msg.answer("Поставок пока нет.")
            return
        lines = [f"📋 Поставок: {len(supplies)}"]
        if state_filter:
            lines[0] += f" (фильтр: {state_filter})"
        for s in supplies:
            lines.append(f"<code>/supply_show {s.id}</code> {s.marketplace.upper()} {s.warehouse[:30]} — <b>{s.state}</b>")
        await msg.answer("\n".join(lines))


# ── /supply_show ─────────────────────────────────────────────────────────────

@router.message(Command("supply_show"))
async def cmd_supply_show(msg: Message, command: CommandObject) -> None:
    try:
        supply_id = int((command.args or "").strip())
    except ValueError:
        await msg.answer("Использование: <code>/supply_show ID</code>")
        return
    with db_session() as session:
        supply = get_supply(session, supply_id)
        if not supply:
            await msg.answer(f"Поставка #{supply_id} не найдена.")
            return
        text = _format_supply_card(supply)
        kb = kb_supply_card(supply.id, supply.state)
    await msg.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("sup_open:"))
async def cb_supply_open(cb: CallbackQuery) -> None:
    supply_id = int(cb.data.split(":", 1)[1])
    with db_session() as session:
        supply = get_supply(session, supply_id)
        if not supply:
            await cb.answer("Не найдена", show_alert=True)
            return
        text = _format_supply_card(supply)
        kb = kb_supply_card(supply.id, supply.state)
    await cb.answer()
    if cb.message:
        await cb.message.answer(text, reply_markup=kb)


# ── /supply_add_item ─────────────────────────────────────────────────────────

@router.message(Command("supply_add_item"))
async def cmd_supply_add_item(msg: Message, command: CommandObject, state: FSMContext) -> None:
    try:
        supply_id = int((command.args or "").strip())
    except ValueError:
        await msg.answer("Использование: <code>/supply_add_item ID</code>")
        return
    with db_session() as session:
        supply = get_supply(session, supply_id)
        if not supply:
            await msg.answer(f"Поставка #{supply_id} не найдена.")
            return
        skus = list_skus(session, limit=80)
    if not skus:
        await msg.answer("Каталог пуст. Сначала добавь SKU.")
        return
    await state.update_data(supply_id=supply_id)
    await state.set_state(SupplyAddItem.sku)
    await msg.answer(
        f"Выбери SKU для поставки #{supply_id}:",
        reply_markup=kb_pick_sku(skus, prefix="add_sku"),
    )


@router.callback_query(F.data.startswith("si_add:"))
async def cb_si_add(cb: CallbackQuery, state: FSMContext) -> None:
    supply_id = int(cb.data.split(":", 1)[1])
    with db_session() as session:
        skus = list_skus(session, limit=80)
    if not skus:
        await cb.answer("Каталог пуст", show_alert=True)
        return
    await state.update_data(supply_id=supply_id)
    await state.set_state(SupplyAddItem.sku)
    await cb.answer()
    if cb.message:
        await cb.message.answer(
            f"Выбери SKU для поставки #{supply_id}:",
            reply_markup=kb_pick_sku(skus, prefix="add_sku"),
        )


@router.callback_query(SupplyAddItem.sku, F.data.startswith("add_sku:"))
async def supply_add_pick_sku(cb: CallbackQuery, state: FSMContext) -> None:
    sku_id = int(cb.data.split(":", 1)[1])
    await state.update_data(sku_id=sku_id)
    await state.set_state(SupplyAddItem.qty)
    await cb.answer()
    if cb.message:
        with db_session() as session:
            sku = get_sku(session, sku_id)
            article = sku.article if sku else "?"
        await safe_edit_or_answer(cb.message, f"Сколько штук <b>{article}</b>?")


@router.message(SupplyAddItem.qty)
async def supply_add_qty(msg: Message, state: FSMContext) -> None:
    try:
        qty = int((msg.text or "").strip())
    except ValueError:
        await msg.answer("Нужно число.")
        return
    data = await state.get_data()
    with db_session() as session:
        items = add_item(session, supply_id=data["supply_id"], sku_id=data["sku_id"], qty_planned=qty)
        kit_added = len(items) > 1
    await state.clear()
    suffix = " (раскрыт набор)" if kit_added else ""
    sid = data['supply_id']
    await msg.answer(
        f"✅ Добавлено в поставку #{sid}: × {qty}{suffix}\n\n"
        f"<code>/supply_show {sid}</code>"
    )


# ── state transitions ─────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("trans:"))
async def cb_transition(cb: CallbackQuery) -> None:
    _, supply_id_s, new_state = cb.data.split(":", 2)
    supply_id = int(supply_id_s)
    with db_session() as session:
        try:
            transition(session, supply_id, new_state, event="manual via bot")
        except ValueError as e:
            await cb.answer(str(e), show_alert=True)
            return
        supply = get_supply(session, supply_id)
        text = _format_supply_card(supply)
        kb = kb_supply_card(supply.id, supply.state)
    await cb.answer(f"Состояние: {new_state}")
    if cb.message:
        await safe_edit_or_answer(cb.message, text, reply_markup=kb)


# ── /supply_delete ────────────────────────────────────────────────────────────

@router.message(Command("supply_delete"))
async def cmd_supply_delete(msg: Message, command: CommandObject) -> None:
    try:
        supply_id = int((command.args or "").strip())
    except ValueError:
        await msg.answer("Использование: <code>/supply_delete ID</code>")
        return
    with db_session() as session:
        supply = session.get(Supply, supply_id)
        if not supply:
            await msg.answer(f"Поставка #{supply_id} не найдена.")
            return
        session.delete(supply)
    await msg.answer(f"🗑 Поставка #{supply_id} удалена.")


@router.callback_query(F.data.startswith("sup_del:"))
async def cb_supply_delete(cb: CallbackQuery) -> None:
    supply_id = int(cb.data.split(":", 1)[1])
    with db_session() as session:
        supply = session.get(Supply, supply_id)
        if not supply:
            await cb.answer("Не найдено", show_alert=True)
            return
        session.delete(supply)
    await cb.answer("Удалено")
    if cb.message:
        await safe_edit_or_answer(cb.message, f"🗑 Поставка #{supply_id} удалена.")
