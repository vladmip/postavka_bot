from datetime import date, timedelta
from typing import List, Optional, Set

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src.warehouses import (
    WB_FOOD_WAREHOUSES, OZON_KNOWN,
    WB_CLUSTERS, OZON_CLUSTERS,
    wb_warehouse_label, wb_cluster_names, wb_warehouses_in_cluster,
    ozon_cluster_names, ozon_warehouses_in_cluster,
)


def kb_marketplace() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🟦 Wildberries", callback_data="mp:wb"),
        InlineKeyboardButton(text="🟧 Ozon", callback_data="mp:ozon"),
    ]])


def kb_clusters(marketplace: str) -> InlineKeyboardMarkup:
    """Кластерный выбор склада. callback = cl_wb:N или cl_oz:N."""
    rows: List[List[InlineKeyboardButton]] = []
    if marketplace == "wb":
        for i, name in enumerate(wb_cluster_names()):
            count = len(wb_warehouses_in_cluster(name))
            rows.append([InlineKeyboardButton(
                text=f"📦 {name} ({count} скл.)",
                callback_data=f"cl_wb:{i}",
            )])
    else:
        for i, name in enumerate(ozon_cluster_names()):
            warehouses = ozon_warehouses_in_cluster(name)
            count = len(warehouses)
            rows.append([InlineKeyboardButton(
                text=f"📦 {name} ({count} скл.)",
                callback_data=f"cl_oz:{i}",
            )])
    rows.append([InlineKeyboardButton(text="✍ Другой склад", callback_data="wh_custom")])
    rows.append([InlineKeyboardButton(text="✖ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_warehouses_in_cluster(marketplace: str, cluster_idx: int) -> InlineKeyboardMarkup:
    """Склады внутри выбранного кластера. callback = wh_wb:N / wh_oz:N (глоб. индекс)."""
    rows: List[List[InlineKeyboardButton]] = []
    back_cb = f"cl_back:{marketplace}"

    if marketplace == "wb":
        cluster_name = wb_cluster_names()[cluster_idx]
        for wh in wb_warehouses_in_cluster(cluster_name):
            try:
                global_idx = WB_FOOD_WAREHOUSES.index(wh)
            except ValueError:
                continue
            label = wb_warehouse_label(wh)
            rows.append([InlineKeyboardButton(text=label, callback_data=f"wh_wb:{global_idx}")])
    else:
        cluster_name = ozon_cluster_names()[cluster_idx]
        warehouses = ozon_warehouses_in_cluster(cluster_name)
        for wh in warehouses:
            try:
                global_idx = OZON_KNOWN.index(wh)
            except ValueError:
                continue
            rows.append([InlineKeyboardButton(text=wh, callback_data=f"wh_oz:{global_idx}")])
        if len(warehouses) > 1:
            rows.append([InlineKeyboardButton(
                text=f"🔀 Все склады кластера {cluster_name}",
                callback_data=f"wh_oz_all:{cluster_idx}",
            )])

    rows.append([InlineKeyboardButton(text="◀ К кластерам", callback_data=back_cb)])
    rows.append([InlineKeyboardButton(text="✖ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def resolve_warehouse(marketplace: str, payload: str) -> Optional[str]:
    """payload = 'N' (глоб. индекс). Возвращает название или None."""
    try:
        idx = int(payload)
    except ValueError:
        return None
    source = WB_FOOD_WAREHOUSES if marketplace == "wb" else OZON_KNOWN
    if 0 <= idx < len(source):
        return source[idx]
    return None


def resolve_ozon_cluster_warehouses(cluster_idx: int) -> List[str]:
    """Все склады Ozon для кластера cluster_idx."""
    try:
        name = ozon_cluster_names()[cluster_idx]
    except IndexError:
        return []
    return ozon_warehouses_in_cluster(name)


def kb_intake_mode() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Поштучно", callback_data="im:piece")],
        [InlineKeyboardButton(text="Коробами", callback_data="im:by_box")],
        [InlineKeyboardButton(text="Без пересчёта", callback_data="im:no_count")],
    ])


def kb_supply_card(supply_id: int, state: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="➕ Добавить SKU", callback_data=f"si_add:{supply_id}")],
        [
            InlineKeyboardButton(text="📥 ТЗ Приёмка", callback_data=f"exp_in:{supply_id}"),
            InlineKeyboardButton(text="📤 ТЗ Отгрузка", callback_data=f"exp_out:{supply_id}"),
        ],
    ]

    transitions = {
        "draft": [("→ Отправлен приёмка", "intake_sent")],
        "intake_sent": [("→ Принят на склад", "intake_done")],
        "intake_done": [("→ Отправлен отгрузка", "shipment_sent")],
        "shipment_sent": [("→ Собрано", "picked")],
        "picked": [("→ Отгружено", "shipped")],
        "shipped": [("→ Принято МП", "accepted")],
        "accepted": [("→ Закрыто", "closed")],
    }
    for label, target in transitions.get(state, []):
        rows.append([InlineKeyboardButton(text=label, callback_data=f"trans:{supply_id}:{target}")])

    rows.append([
        InlineKeyboardButton(text="🗑 Отменить поставку", callback_data=f"trans:{supply_id}:cancelled"),
        InlineKeyboardButton(text="❌ Удалить", callback_data=f"sup_del:{supply_id}"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_pick_supply(supplies: list) -> InlineKeyboardMarkup:
    rows = []
    for s in supplies[:30]:
        rows.append([InlineKeyboardButton(
            text=f"#{s.id} {s.marketplace.upper()} {s.warehouse[:30]} [{s.state}]",
            callback_data=f"bind:{s.id}",
        )])
    rows.append([InlineKeyboardButton(text="✖ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_pick_sku(skus: list, prefix: str = "pick_sku") -> InlineKeyboardMarkup:
    rows = []
    for s in skus[:40]:
        rows.append([InlineKeyboardButton(
            text=f"{s.article} — {s.name[:30]}",
            callback_data=f"{prefix}:{s.id}",
        )])
    rows.append([InlineKeyboardButton(text="✖ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_slot_choice(marketplace: str = "") -> InlineKeyboardMarkup:
    """После выбора склада — спросить про дату слота."""
    rows = [
        [InlineKeyboardButton(text="📅 Календарь (выбор тапом)", callback_data="slot:cal")],
        [InlineKeyboardButton(text="✍ Ввести вручную", callback_data="slot:manual")],
        [InlineKeyboardButton(text="⏭ Без даты (просто draft)", callback_data="slot:skip")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


_RU_WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
_RU_MONTHS = ["", "янв", "фев", "мар", "апр", "май", "июн",
              "июл", "авг", "сен", "окт", "ноя", "дек"]


def kb_dates_picker(
    selected_offsets: Optional[Set[int]] = None,
    days_ahead: int = 14,
    min_offset: int = 0,
) -> InlineKeyboardMarkup:
    """Календарная клавиатура: 14 кнопок дат от сегодня + min_offset.

    callback = 'dp:N' где N = офсет от сегодня (0..days_ahead-1).
    Multi-select: тап = toggle. ✓ помечает выбранные.
    'dp_ok' — подтвердить, 'dp_skip' — без даты.
    Если min_offset > 0 — даты раньше отображаются как 🚫 (callback dp_lock).
    """
    selected_offsets = selected_offsets or set()
    today = date.today()

    rows: List[List[InlineKeyboardButton]] = []
    row: List[InlineKeyboardButton] = []
    for n in range(days_ahead):
        d = today + timedelta(days=n)
        wd = _RU_WEEKDAYS[d.weekday()]
        text = f"{d.day} {_RU_MONTHS[d.month]} {wd}"
        if n in selected_offsets:
            text = f"✓ {text}"
        if n < min_offset:
            text = f"🚫 {d.day}.{d.month:02d}"
            row.append(InlineKeyboardButton(text=text, callback_data="dp_lock"))
        else:
            row.append(InlineKeyboardButton(text=text, callback_data=f"dp:{n}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    n_sel = len(selected_offsets)
    confirm_text = f"✅ Подтвердить ({n_sel})" if n_sel else "✅ Подтвердить"
    rows.append([InlineKeyboardButton(text=confirm_text, callback_data="dp_ok")])
    rows.append([
        InlineKeyboardButton(text="⏭ Без даты", callback_data="dp_skip"),
        InlineKeyboardButton(text="✖ Отмена", callback_data="cancel"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)
