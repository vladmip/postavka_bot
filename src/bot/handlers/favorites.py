"""Любимые точки кроссдока — управление списком (FBO drop-off хабы, ПВЗ, ФФ, РФЦ).

Меню → ⭐ Точки кроссдока:
  - Список существующих + кнопки удалить
  - ➕ Добавить (ввод имени/части → поиск в кэше Ozon → подтверждение)
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import List, Optional

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from src.bot.helpers import safe_edit_or_answer
from src.config import APIKEY_OZON, CLIENT_ID_OZON, OZON_PROXY_URL
from src.db.models import FavoriteCrossdockPoint
from src.db.session import db_session
from src.integrations import OzonClient
from src.integrations.ozon_api import OzonAPIError

router = Router()
logger = logging.getLogger("bot.favorites")

# Safety-belt: module-level кэш последних результатов поиска. Используется если
# FSM state потерялся между search'ем и кликом (timeout, edit, navigation).
# Хранит до ~200 wh_id → match-dict.
_RECENT_MATCHES: dict = {}


def _remember_matches(matches: list) -> None:
    """Сохранить результаты поиска в модульный кэш, для последующего pick."""
    global _RECENT_MATCHES
    if len(_RECENT_MATCHES) > 500:
        _RECENT_MATCHES.clear()
    for m in matches:
        try:
            _RECENT_MATCHES[int(m["wh_id"])] = m
        except (KeyError, ValueError, TypeError):
            pass


class FavAdd(StatesGroup):
    query = State()


def _list_favorites() -> List[FavoriteCrossdockPoint]:
    with db_session() as session:
        rows = (
            session.query(FavoriteCrossdockPoint)
            .order_by(
                FavoriteCrossdockPoint.use_count.desc(),
                FavoriteCrossdockPoint.last_used_at.desc(),
                FavoriteCrossdockPoint.created_at.desc(),
            )
            .all()
        )
        return [
            FavoriteCrossdockPoint(
                id=r.id, name=r.name, warehouse_id=r.warehouse_id,
                point_type=r.point_type, notes=r.notes, use_count=r.use_count,
                last_used_at=r.last_used_at, created_at=r.created_at,
            )
            for r in rows
        ]


def _render_list_kb() -> tuple:
    favs = _list_favorites()
    rows: List[List[InlineKeyboardButton]] = []
    if not favs:
        text = (
            "⭐ <b>Любимые точки кроссдока</b>\n\n"
            "<i>Пока пусто. Добавь точку отгрузки (хаб/ФФ/ПВЗ/РФЦ) — "
            "она будет наверху списка при выборе кроссдока.</i>"
        )
    else:
        lines = ["⭐ <b>Любимые точки кроссдока</b>\n"]
        for f in favs:
            type_str = _type_label(f.point_type or "")
            usage = f" · {f.use_count}×" if f.use_count else ""
            lines.append(f"• <b>{f.name}</b> — {type_str}{usage}")
        text = "\n".join(lines)
        for f in favs[:20]:
            rows.append([InlineKeyboardButton(
                text=f"🗑 {f.name[:30]}", callback_data=f"fav:del:{f.id}",
            )])
    rows.append([InlineKeyboardButton(text="➕ Добавить", callback_data="fav:add")])
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:home")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "menu:favorites")
async def cb_menu_favorites(cb: CallbackQuery) -> None:
    await cb.answer()
    if not cb.message:
        return
    text, kb = _render_list_kb()
    await safe_edit_or_answer(cb.message, text, reply_markup=kb)


@router.callback_query(F.data.startswith("fav:del:"))
async def cb_fav_del(cb: CallbackQuery) -> None:
    fav_id = int(cb.data.split(":")[2])
    with db_session() as session:
        row = session.get(FavoriteCrossdockPoint, fav_id)
        if row:
            name = row.name
            session.delete(row)
            await cb.answer(f"Удалено: {name}")
        else:
            await cb.answer("Уже нет", show_alert=True)
    if cb.message:
        text, kb = _render_list_kb()
        await safe_edit_or_answer(cb.message, text, reply_markup=kb)


@router.callback_query(F.data == "fav:add")
async def cb_fav_add(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    if not cb.message:
        return
    await state.set_state(FavAdd.query)
    await safe_edit_or_answer(
        cb.message,
        "✏ <b>Добавить точку</b>\n\n"
        "Напиши часть имени точки отгрузки (хаба / ФФ / ПВЗ / РФЦ).\n"
        "Например: <code>Щербинка</code>, <code>ХОРУГВИНО</code>, "
        "<code>Внуково</code>.\n\nИли отправь <code>id</code> напрямую — "
        "число warehouse_id из Ozon API.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✖ Отмена", callback_data="menu:favorites")],
        ]),
    )


@router.message(FavAdd.query)
async def msg_fav_add_query(msg: Message, state: FSMContext) -> None:
    query = (msg.text or "").strip()
    if not query:
        await msg.answer("Пустой запрос — попробуй ещё раз.")
        return

    # Если число — пробуем сразу как warehouse_id
    if query.isdigit() and len(query) >= 6:
        wh_id = int(query)
        # Попробуем найти имя в cluster_list
        name = await _resolve_warehouse_name(wh_id) or f"#{wh_id}"
        await _confirm_add(msg, state, name, wh_id)
        return

    # Иначе ищем по имени в каталоге Ozon-точек
    matches = await _search_warehouses(query)
    if not matches:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✖ Отмена", callback_data="menu:favorites")],
        ])
        await msg.answer(
            f"❌ Не нашёл точку по запросу «{query}» ни в FBO-кластерах, ни в FBS drop-off.\n\n"
            f"Что можно сделать:\n"
            f"• Уточнить часть имени (например <code>Хоругвино</code>, <code>Домодедово</code>).\n"
            f"• Отправить <code>warehouse_id</code> числом — если знаешь.\n"
            f"• Из Ozon ЛК: открой нужную точку в карточке создания поставки, "
            f"в URL/payload часто видно warehouse_id.",
            reply_markup=kb,
        )
        return

    # Сортируем по приоритету типа (крупные склады первые, ПВЗ последние)
    matches.sort(key=lambda m: (_type_priority(m.get("type", "")), m["name"]))

    # Сохраняем и в state (для пагинации), и в модульный кэш (failsafe для pick)
    await state.update_data(fav_matches=matches, fav_query=query, fav_offset=0)
    _remember_matches(matches)
    await _render_fav_page(msg, state, offset=0)


PAGE_SIZE = 8


def _type_priority(t: str) -> int:
    """Чем меньше — тем выше в списке. Кроссдок и РФЦ сверху, ПВЗ снизу."""
    t = (t or "").upper()
    if "CROSS_DOCK" in t:
        return 1
    if "FULL_FILLMENT" in t:
        return 2
    if "SORTING_CENTER" in t or "DISTRIBUTION_CENTER" in t or t == "SC":
        return 3
    if t == "DELIVERY_POINT":
        return 4  # generic «точка сдачи» — между СЦ и ПВЗ
    if t in ("PPZ",):
        return 5
    if "ORDERS_RECEIVING_POINT" in t or t in ("PVZ",):
        return 6
    return 9


# Понятные русские подписи типов + флаг «крупная точка» (показываем (Рекомендуется))
# Маркер (Рекомендуется) — только для крупных точек, где принимают серьёзные
# партии. DELIVERY_POINT у Ozon — это generic «точка сдачи», туда попадают
# и нормальные drop-off-хабы, и мелкие ПВЗ-стиль точки (как МО_АПРЕЛЕВКА_24),
# поэтому маркер не ставим, чтобы не обмануть.
_TYPE_LABEL = {
    "CROSS_DOCK": ("Кроссдок", True),
    "FULL_FILLMENT": ("РФЦ", True),
    "SORTING_CENTER": ("СЦ", True),
    "DISTRIBUTION_CENTER": ("ДЦ", True),
    "DELIVERY_POINT": ("Точка сдачи", False),
    "ORDERS_RECEIVING_POINT": ("ПВЗ", False),
    "PVZ": ("ПВЗ", False),
    "PPZ": ("ППЗ", False),
    "SC": ("СЦ", True),
}


def _type_label(t: str) -> str:
    """Превратить API-enum в человеческую подпись с маркером (Рекомендуется)
    для крупных точек."""
    t = (t or "").upper()
    label, is_rec = _TYPE_LABEL.get(t, (t or "?", False))
    return f"{label} (Рекомендуется)" if is_rec else label


def _common_prefix_len(strings: List[str]) -> int:
    """Длина общего префикса нескольких строк. Округляем назад до границы слова
    (по символам `_`, ` `, `,`, `.`) — чтобы не резать посередине слова и не
    оставлять обрывков типа «_КРОССДОКИНГ»."""
    if not strings:
        return 0
    first = strings[0]
    raw = 0
    for i, ch in enumerate(first):
        for s in strings[1:]:
            if i >= len(s) or s[i] != ch:
                raw = i
                break
        else:
            continue
        break
    else:
        raw = len(first)
    # Откатываемся к последнему разделителю
    if raw == 0:
        return 0
    boundary_chars = {"_", " ", ",", ".", "-", "/"}
    while raw > 0 and first[raw - 1] not in boundary_chars:
        raw -= 1
    return raw


async def _render_fav_page(msg: Message, state: FSMContext, offset: int) -> None:
    """Отрисовать страницу результатов поиска с навигацией."""
    data = await state.get_data()
    matches: List[dict] = data.get("fav_matches") or []
    query: str = data.get("fav_query") or ""
    if not matches:
        await msg.answer("⚠ Результаты поиска утеряны (рестарт?). Открой меню → Точки кроссдока → Добавить.")
        return

    total = len(matches)
    offset = max(0, min(offset, total - 1))
    page = matches[offset:offset + PAGE_SIZE]
    names = [m["name"] for m in page]
    # Срезаем общий префикс только если на странице много вариантов (≥4) —
    # при 2-3 коротких полные имена помещаются и читаются лучше.
    common = _common_prefix_len(names) if len(page) >= 4 else 0

    rows: List[List[InlineKeyboardButton]] = []
    for m in page:
        short = m["name"][common:].strip(" ,;.") or m["name"]
        type_label = _type_label(m.get("type", ""))
        # Telegram кнопки до ~64 char — режем умеренно
        rows.append([InlineKeyboardButton(
            text=f"{short[:40]} · {type_label}"[:62],
            callback_data=f"fav:pick:{m['wh_id']}",
        )])

    # Навигация
    nav: List[InlineKeyboardButton] = []
    if offset > 0:
        nav.append(InlineKeyboardButton(
            text="◀ Назад",
            callback_data=f"fav:page:{max(0, offset - PAGE_SIZE)}",
        ))
    if offset + PAGE_SIZE < total:
        nav.append(InlineKeyboardButton(
            text="Вперёд ▶",
            callback_data=f"fav:page:{offset + PAGE_SIZE}",
        ))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="✖ Отмена", callback_data="menu:favorites")])

    common_text = page[0]["name"][:common].strip(" ,;.") if common > 0 else ""
    prefix_hint = f"\n<i>Общая часть: {common_text}</i>" if common_text else ""
    page_n = (offset // PAGE_SIZE) + 1
    pages_total = (total + PAGE_SIZE - 1) // PAGE_SIZE
    text = (
        f"Найдено по «{query}»: <b>{total}</b> точек "
        f"(стр. {page_n}/{pages_total}, отсортировано: крупные → ПВЗ){prefix_hint}\n\n"
        "Выбери нужную:"
    )
    await safe_edit_or_answer(msg, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("fav:page:"))
async def cb_fav_page(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    try:
        offset = int(cb.data.split(":")[2])
    except (ValueError, IndexError):
        offset = 0
    if cb.message:
        await _render_fav_page(cb.message, state, offset=offset)


@router.callback_query(F.data.startswith("fav:pick:"))
async def cb_fav_pick(cb: CallbackQuery, state: FSMContext) -> None:
    wh_id = int(cb.data.split(":")[2])
    # 1) state — обычный путь
    data = await state.get_data()
    matches = data.get("fav_matches") or []
    selected = next((m for m in matches if int(m.get("wh_id", 0)) == wh_id), None)
    # 2) модульный кэш — на случай потери state (timeout / другой handler сбросил)
    if not selected:
        selected = _RECENT_MATCHES.get(wh_id)
    # 3) cluster_list cache — последний фолбэк
    if selected:
        name = selected.get("name") or f"#{wh_id}"
        point_type = selected.get("type") or ""
    else:
        name = await _resolve_warehouse_name(wh_id) or f"#{wh_id}"
        point_type = ""
    await cb.answer()
    if cb.message:
        await _confirm_add(cb.message, state, name, wh_id, point_type)


async def _confirm_add(
    msg: Message, state: FSMContext, name: str, wh_id: int, point_type: str = "",
) -> None:
    """Сохраняет в БД и возвращает к списку любимых."""
    with db_session() as session:
        exists = (
            session.query(FavoriteCrossdockPoint)
            .filter(FavoriteCrossdockPoint.warehouse_id == wh_id)
            .first()
        )
        if exists:
            await msg.answer(f"ℹ Точка <b>{exists.name}</b> уже в любимых.")
        else:
            session.add(FavoriteCrossdockPoint(
                name=name, warehouse_id=wh_id, point_type=point_type or None,
            ))
    await state.clear()
    text, kb = _render_list_kb()
    type_part = f" ({_type_label(point_type)})" if point_type else ""
    await msg.answer(
        f"✅ Добавлено: <b>{name}</b>{type_part}\n\n" + text,
        reply_markup=kb,
    )


def _iter_warehouses(clusters: List[dict]):
    """Все warehouses из кластеров: cluster.logistic_clusters[].warehouses[]."""
    for cl in clusters:
        cl_name = cl.get("name") or ""
        for lc in (cl.get("logistic_clusters") or []):
            for wh in (lc.get("warehouses") or []):
                yield cl_name, wh


async def _search_warehouses(query: str) -> List[dict]:
    """Поиск точек для FBO кроссдока:
      1) /v1/warehouse/fbo/list (filter=CROSSDOCK) — авторитетный список Ozon,
         именно то что доступно для кроссдока в этом аккаунте.
      2) fallback: /v1/cluster/list (локальный кэш) — на случай если fbo/list
         пустой или недоступен.
    FBS drop-off НЕ используется — ПВЗ для FBO-кроссдока неприменимы и захламляют выдачу.
    """
    if not APIKEY_OZON or not CLIENT_ID_OZON:
        return []
    q = query.lower().strip()
    oz = OzonClient(CLIENT_ID_OZON, APIKEY_OZON, proxy=OZON_PROXY_URL)
    matches: List[dict] = []
    seen_ids = set()

    # 1) FBO drop-off для кроссдока. Только CREATE_TYPE_CROSSDOCK — DIRECT-РФЦ
    # сюда не пускаем, они для прямой поставки, не для кроссдока (даже если
    # имя похожее). Ozon-кабинет сам определяет какие точки разрешены.
    if len(q) >= 4:
        try:
            points = await oz.warehouse_fbo_list(
                supply_types=["CREATE_TYPE_CROSSDOCK"],
                search=query,
            )
            for p in points[:50]:
                wid = int(p.get("warehouse_id") or 0)
                if not wid or wid in seen_ids:
                    continue
                seen_ids.add(wid)
                wtype = (p.get("warehouse_type") or "").replace("WAREHOUSE_TYPE_", "")
                matches.append({
                    "wh_id": wid,
                    "name": p.get("name") or p.get("address") or f"#{wid}",
                    "type": wtype or "?",
                    "cluster": "FBO",
                })
        except OzonAPIError as e:
            logger.warning("fbo/list failed: %s", e)

    # 2) Локальный кэш cluster_list — на случай если fbo/list ничего не вернул
    if not matches:
        try:
            clusters = await oz.cluster_list(allow_stale=True)
            for cl_name, wh in _iter_warehouses(clusters):
                name = (wh.get("name") or "")
                wtype = (wh.get("type") or "").upper()
                # для крос-дока релевантны CROSS_DOCK/SORTING_CENTER/DELIVERY_POINT
                if name and q in name.lower() and wtype in {
                    "CROSS_DOCK", "SORTING_CENTER", "DELIVERY_POINT",
                    "DISTRIBUTION_CENTER", "FULL_FILLMENT",
                }:
                    wid = int(wh.get("warehouse_id") or 0)
                    if wid and wid not in seen_ids:
                        seen_ids.add(wid)
                        matches.append({
                            "wh_id": wid,
                            "name": name,
                            "type": wtype,
                            "cluster": cl_name,
                        })
        except OzonAPIError as e:
            logger.warning("cluster_list failed: %s", e)

    return matches


async def _resolve_warehouse_name(wh_id: int) -> Optional[str]:
    """Найти имя по warehouse_id в кэше Ozon-кластеров."""
    if not APIKEY_OZON or not CLIENT_ID_OZON:
        return None
    oz = OzonClient(CLIENT_ID_OZON, APIKEY_OZON, proxy=OZON_PROXY_URL)
    try:
        clusters = await oz.cluster_list(allow_stale=True)
    except OzonAPIError:
        return None
    for _cl_name, wh in _iter_warehouses(clusters):
        if int(wh.get("warehouse_id") or 0) == wh_id:
            return wh.get("name")
    return None


def bump_use(warehouse_id: int) -> None:
    """Увеличить счётчик использования. Зовём после успешной брони на этом wh."""
    with db_session() as session:
        row = (
            session.query(FavoriteCrossdockPoint)
            .filter(FavoriteCrossdockPoint.warehouse_id == warehouse_id)
            .first()
        )
        if row:
            row.use_count += 1
            row.last_used_at = datetime.utcnow()
