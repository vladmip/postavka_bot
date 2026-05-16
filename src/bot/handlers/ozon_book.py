"""Создание поставки FBO Ozon через Draft API.

Workflow:
  1. /ozon_book <ship_id> — стартует
  2. Бот собирает items по Ozon-кластерам из заявки
  3. Спрашивает тип (CROSSDOCK / DIRECT)
  4. POST /v1/draft/create
  5. Polls /v1/draft/create/info до status=DONE
  6. POST /v1/draft/timeslot/info — показывает доступные drop-off и слоты
  7. Пользователь тапает слот → POST /v1/draft/supply/create
  8. Polls /v1/draft/supply/create/info → supply создана в Ozon ЛК
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from aiogram import Router, F
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from src.bot.helpers import safe_edit_or_answer, send_long, progress_start, progress_add, progress_reset
from src.config import OZON_PROXY_URL
from src.db.models import ShipmentRequest, ShipmentItem, Sku
from src.db.session import db_session
from src.integrations import OzonClient, OzonAPIError
from src.services.shipment_service import get_shipment_request
from src.services.slot_hunter import _ozon_cluster_to_name, _normalize
from src.services.user_service import (
    current_user_id_from,
    get_ozon_client_for,
    get_ozon_creds,
)


async def _ozon_client_from_state(state: FSMContext) -> Optional[OzonClient]:
    """Помощник: вытаскивает ob_tg_id из state и возвращает готовый OzonClient
    для этого юзера. None если tg_id нет или у юзера нет Ozon-кред."""
    data = await state.get_data()
    tg_id = data.get("ob_tg_id")
    if not tg_id:
        return None
    with db_session() as s:
        return get_ozon_client_for(s, int(tg_id))


def _ozon_client_for_tg(tg_id: int) -> Optional[OzonClient]:
    """Помощник для не-state callsites — берёт креды юзера и собирает OzonClient."""
    with db_session() as s:
        return get_ozon_client_for(s, tg_id)


_NO_OZON_KEYS_MSG = "⚠ Ozon-ключи не настроены. Открой /start → «Добавить Ozon»."

router = Router()
logger = logging.getLogger("bot.ozon_book")


class OzonBook(StatesGroup):
    pick_type = State()
    pick_warehouse = State()
    pick_dropoff = State()       # CROSSDOCK: выбор drop-off-точки для каждого кластера
    pick_dropoff_input = State() # CROSSDOCK: ввод имени для поиска drop-off
    pick_slot = State()


# ── Курированный приоритет складов FF по кластерам ─────────────────────────
# Бот «🎲 Любой» = первый из списка, доступный в API.
# Для ИП Баковец (ЛЕБЕР в Домодедово) Домодедово_РФЦ — оптимальный.
_WAREHOUSE_PRIORITY: Dict[str, List[str]] = {
    "москва": ["ДОМОДЕДОВО", "ХОРУГВИНО", "ПУШКИНО", "СОФЬИНО", "ЖУКОВСКИЙ", "ВАТУТИНКИ"],
    # Для других кластеров без курирования — используется алфавит.
}

# Слова в имени склада, которые означают «не для нашего использования»
# (негабарит, КГТ, шины, аптека, фотостудия, паллетный — узко-специализированные).
_WAREHOUSE_BLACKLIST_WORDS = (
    "НЕГАБАРИТ", "КГТ", "ШИНЫ", "АПТЕКА", "ВЕТАПТЕКА",
    "ФОТОСТУДИЯ", "ПАЛЛЕТНЫЙ", "КРОССДОКИНГ",
)


def _get_cluster_ff_warehouses(cluster_name: str) -> List[Dict]:
    """Возвращает список FF-складов кластера (только базовые РФЦ, без негабарита/КГТ/etc).
    Сортирует по курированному приоритету.

    Возвращает [{wh_id: int, name: str}, ...]
    """
    from src.integrations._cache import cache_get
    cached = cache_get("ozon_clusters", max_age_sec=86400 * 7)  # читаем и старый кэш
    if not cached:
        return []
    target_norm = _normalize(cluster_name)
    cluster = None
    for cl in cached:
        if _normalize(cl.get("name", "")) == target_norm or target_norm in _normalize(cl.get("name", "")):
            cluster = cl
            break
    if not cluster:
        return []

    # Собираем все FF-склады
    warehouses: List[Dict] = []
    for lc in (cluster.get("logistic_clusters") or []):
        for w in (lc.get("warehouses") or []):
            if w.get("type") != "FULL_FILLMENT":
                continue
            name = (w.get("name") or "").strip()
            if not name:
                continue
            up = name.upper()
            if any(bad in up for bad in _WAREHOUSE_BLACKLIST_WORDS):
                continue
            warehouses.append({
                "wh_id": w.get("warehouse_id"),
                "name": name,
            })

    # Сортируем по приоритету
    priority = _WAREHOUSE_PRIORITY.get(target_norm.split()[0] if target_norm else "", [])

    def _key(w: Dict) -> tuple:
        up = w["name"].upper()
        for i, prefix in enumerate(priority):
            if prefix.upper() in up:
                return (0, i, up)
        return (1, 0, up)  # все непримеченные — после, алфавит

    warehouses.sort(key=_key)
    return warehouses


# ── Auto-poll state (фоновые задачи периодического опроса timeslot/info) ─────
# Ключ — rid заявки. Значение — asyncio.Task + ChatID получателя нотификаций.
_AUTO_POLL_TASKS: Dict[int, asyncio.Task] = {}
# Кэш найденных слотов: token → детали, чтобы callback obfslot:<token> работал
# без зависимости от FSM state (auto-poll присылает результат когда пользователь
# мог уже выйти из мастера).
_FOUND_SLOTS: Dict[str, Dict] = {}

# Single-flight lock: rid'ы по которым УЖЕ идёт supply/create.
# Защищает от двойного запроса при двойном тапе слота (Telegram задержки + человек).
_BOOKING_IN_FLIGHT: set = set()

# Single-flight lock на уровне «весь wizard для заявки»: TTL 30 минут.
# Защищает от двойного тапа «🚀 Создать поставку Ozon» в карточке заявки.
# Значение — timestamp начала. Авто-снимается через 30 мин.
import time as _time
_WIZARD_IN_FLIGHT: Dict[int, float] = {}
_WIZARD_TTL_SEC = 30 * 60

# Второй уровень: lock на конкретный шаг «создание drafts + scoring». Wizard-lock
# защищает от повторного входа через карточку заявки, но НЕ от повторного входа
# через callbacks drop-off picker'а (юзер кликнул пагинацию / «◀ К выбору» /
# повторно «✓ применить ко всем» — два потока конкурентно создавали drafts).
# TTL 10 мин — на случай если функция упадёт без release.
_DRAFTS_CREATING: Dict[int, float] = {}
_DRAFTS_CREATING_TTL_SEC = 10 * 60


def _wizard_acquire(rid: int) -> bool:
    """True если lock взят, False если уже занят (рестарт wizard'а блокируется)."""
    now = _time.time()
    # GC: убираем просроченные locks (защита от висяков)
    for k in list(_WIZARD_IN_FLIGHT.keys()):
        if now - _WIZARD_IN_FLIGHT[k] > _WIZARD_TTL_SEC:
            _WIZARD_IN_FLIGHT.pop(k, None)
    if rid in _WIZARD_IN_FLIGHT:
        return False
    _WIZARD_IN_FLIGHT[rid] = now
    return True


def _wizard_release(rid: int) -> None:
    _WIZARD_IN_FLIGHT.pop(rid, None)


def _drafts_creating_acquire(rid: int) -> bool:
    """True если шаг «создание drafts + scoring» можно начинать, False если уже
    выполняется (повторный entry из drop-off picker / case `idx>=len(clusters)`)."""
    now = _time.time()
    for k in list(_DRAFTS_CREATING.keys()):
        if now - _DRAFTS_CREATING[k] > _DRAFTS_CREATING_TTL_SEC:
            _DRAFTS_CREATING.pop(k, None)
    if rid in _DRAFTS_CREATING:
        return False
    _DRAFTS_CREATING[rid] = now
    return True


def _drafts_creating_release(rid: int) -> None:
    _DRAFTS_CREATING.pop(rid, None)


async def _release_wizard_for_state(state: FSMContext) -> None:
    """Снять wizard-lock на основе rid из текущего FSM-state. Используется
    в early-exits внутри multi-step wizard'а, чтобы lock не висел до TTL."""
    try:
        data = await state.get_data()
        rid = data.get("ob_rid")
        if rid:
            _wizard_release(int(rid))
    except Exception:
        pass


# ── helpers ─────────────────────────────────────────────────────────────────


def _parse_v2_timeslots(
    ts: Dict, fallback_wh_id: Optional[int] = None, fallback_wh_name: str = "",
) -> List[Dict]:
    """Парсит ответ /v2/draft/timeslot/info в плоский список слотов.

    v2 структура (для 1 склада через selected_cluster_warehouses):
      {"result": {"drop_off_warehouse_timeslots": {"days": [
        {"date_in_timezone": "...", "timeslots": [
          {"from_in_timezone": "...", "to_in_timezone": "..."}, ...
        ]}, ...
      ]}}}
    v1 структура (legacy, массив warehouse-объектов на верхнем уровне) — тоже поддерживаем.

    Возвращает: [{"warehouse_id", "warehouse_name", "from", "to"}, ...]
    """
    out: List[Dict] = []
    # Сначала пробуем v2-структуру под result
    result = ts.get("result") if isinstance(ts.get("result"), dict) else ts
    wh_data = result.get("drop_off_warehouse_timeslots") if isinstance(result, dict) else None

    # v2: 1 объект {days: [...]}
    if isinstance(wh_data, dict):
        wh_id = wh_data.get("drop_off_warehouse_id") or wh_data.get("warehouse_id") or fallback_wh_id or 0
        wh_name = wh_data.get("warehouse_name") or fallback_wh_name or f"#{wh_id}"
        for day in (wh_data.get("days") or []):
            for slot in (day.get("timeslots") or []):
                out.append({
                    "warehouse_id": int(wh_id) if wh_id else 0,
                    "warehouse_name": wh_name,
                    "from": slot.get("from_in_timezone") or slot.get("from") or "",
                    "to": slot.get("to_in_timezone") or slot.get("to") or "",
                })
        return out

    # v1: массив warehouse-объектов
    if isinstance(wh_data, list):
        for wh in wh_data:
            wh_id = wh.get("drop_off_warehouse_id") or wh.get("warehouse_id") or fallback_wh_id or 0
            wh_name = wh.get("warehouse_name") or fallback_wh_name or f"#{wh_id}"
            for day in (wh.get("days") or []):
                for slot in (day.get("timeslots") or []):
                    out.append({
                        "warehouse_id": int(wh_id) if wh_id else 0,
                        "warehouse_name": wh_name,
                        "from": slot.get("from_in_timezone") or slot.get("from") or "",
                        "to": slot.get("to_in_timezone") or slot.get("to") or "",
                    })
        return out

    return out


async def _validate_skus_in_current_account(
    oz: OzonClient, items_to_check: List[int]
) -> Tuple[List[int], Dict[int, str]]:
    """Pre-check: проверяет, что все ozon_sku из items_to_check реально существуют
    в текущем Ozon-кабинете. Защита от стейлых артикулов с другого аккаунта.

    Возвращает (missing_skus, sku_to_offer_id) — missing_skus это те, которые
    отсутствуют в кабинете. sku_to_offer_id — для известных, чтобы показать
    пользователю «sku=123 = offer_id=KINDER».
    """
    try:
        prods = await oz.product_list(limit=5000)
        ids = [p.get("product_id") for p in prods if p.get("product_id")]
        if not ids:
            return list(items_to_check), {}
        infos = await oz.product_info_list(ids)
    except OzonAPIError as e:
        logger.warning("pre-check sku validation failed: %s", e)
        # Не блокируем при ошибке API на pre-check — пусть основной флоу сам решает
        return [], {}

    valid: Dict[int, str] = {}
    for it in infos:
        offer_id = it.get("offer_id") or ""
        v = it.get("sku") or it.get("fbo_sku") or it.get("fbs_sku") or it.get("product_id")
        try:
            v = int(v) if v else None
        except (ValueError, TypeError):
            v = None
        if v:
            valid[v] = offer_id

    missing = [s for s in items_to_check if s not in valid]
    return missing, valid


def _build_items_for_cluster(req: ShipmentRequest, cluster: str) -> Tuple[List[Dict], List[str]]:
    """Собрать items для draft из заявки. Использует ozon_product.sku (числовой).

    Возвращает (items, missing_articles).
    items: [{"sku": int, "quantity": int}]  ← Ozon API требует sku.
    """
    items: List[Dict] = []
    missing: List[str] = []
    by_sku: Dict[int, int] = {}
    for it in req.items:
        if it.marketplace != "ozon" or it.cluster != cluster:
            continue
        op = it.ozon_product
        if not op or not op.sku:
            missing.append(it.raw_article)
            continue
        by_sku[op.sku] = by_sku.get(op.sku, 0) + it.qty
    for ozon_sku, qty in by_sku.items():
        items.append({"sku": ozon_sku, "quantity": qty})
    return items, missing


async def _resolve_ozon_cluster_id(oz: OzonClient, cluster_name_local: str) -> Optional[int]:
    """Найти macrolocal_cluster_id у Ozon API по нашему имени кластера.

    Ozon API теперь отдаёт кластеры по городам (Саратов, Тюмень, Уфа,
    Ярославль, Махачкала, Калининград и т.п. — все отдельно). Поэтому
    матчим напрямую по cluster_list без hardcoded mappings.
    """
    if not cluster_name_local:
        return None
    try:
        clusters = await oz.cluster_list()
    except OzonAPIError as e:
        logger.warning("cluster_list failed: %s", e)
        return None
    target_norm = _normalize(cluster_name_local)
    # 1) Точное совпадение нормализованных имён
    for cl in clusters:
        if _normalize(cl.get("name") or "") == target_norm:
            mcid = cl.get("macrolocal_cluster_id") or cl.get("id")
            try:
                return int(mcid)
            except (ValueError, TypeError):
                return None
    # 2) Подстрочное совпадение («Саратов» ↔ «Саратов и Поволжье», если такие есть)
    for cl in clusters:
        cname_norm = _normalize(cl.get("name") or "")
        if not cname_norm:
            continue
        if target_norm in cname_norm or cname_norm in target_norm:
            mcid = cl.get("macrolocal_cluster_id") or cl.get("id")
            try:
                return int(mcid)
            except (ValueError, TypeError):
                return None
    return None


async def _wait_draft_ready(oz: OzonClient, op_id: str, max_attempts: int = 30) -> Dict:
    """Polls /v1/draft/create/info до status DONE/FAILED. Пауза 3с между попытками."""
    for i in range(max_attempts):
        await asyncio.sleep(3 if i > 0 else 2)  # пауза ДО запроса (после предыдущего)
        try:
            info = await oz.draft_create_info(op_id)
        except OzonAPIError as e:
            if "429" in str(e):
                # ретрай уже внутри клиента, если всё равно 429 — ждём дольше
                await asyncio.sleep(5)
                continue
            raise
        status = info.get("status", "")
        if status in {"CALCULATION_STATUS_SUCCESS", "STATUS_DONE", "DONE", "SUCCESS"}:
            return info
        if "FAIL" in status.upper():
            return info
    return {"status": "TIMEOUT"}


# ── /ozon_book — wizard ─────────────────────────────────────────────────────

@router.message(Command("ozon_book"))
async def cmd_ozon_book(msg: Message, command: CommandObject, state: FSMContext) -> None:
    try:
        rid = int((command.args or "").strip())
    except ValueError:
        await msg.answer("Использование: <code>/ozon_book ID</code>")
        return
    tg_id = current_user_id_from(msg)
    if tg_id is None:
        return
    await _start_ozon_book_wizard(msg, state, rid, tg_id)


@router.callback_query(F.data.startswith("ozon_book_card:"))
async def cb_ozon_book_from_card(cb: CallbackQuery, state: FSMContext) -> None:
    """Триггер /ozon_book из карточки заявки. Формат: ozon_book_card:<rid> или ozon_book_card:<rid>:<mode>.
    Mode: 'direct' (default) | 'cross'.

    Single-flight: если wizard уже бежит для этой заявки — игнорируем повторный
    тап (anti-double-click). Кнопки на исходной карточке сразу гасим чтобы
    юзер не мог тапнуть второй раз пока мастер думает.
    """
    parts = cb.data.split(":")
    rid = int(parts[1])
    mode = parts[2] if len(parts) >= 3 else "direct"
    if not _wizard_acquire(rid):
        await cb.answer(
            f"⏳ Ozon-мастер для поставки #{rid} уже запущен. Подожди завершения.",
            show_alert=True,
        )
        return
    await cb.answer(f"Запускаю Ozon-мастер ({mode.upper()})…")
    tg_id = current_user_id_from(cb)
    if tg_id is None:
        _wizard_release(rid)
        return
    # Гасим кнопки на карточке чтобы исключить повторный клик.
    if cb.message:
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        try:
            launched = await _start_ozon_book_wizard(cb.message, state, rid, tg_id, mode=mode)
        except Exception:
            _wizard_release(rid)
            raise
        if not launched:
            # Early exit (нет ключей/нет дат/всё забронировано) — снять lock сразу,
            # иначе юзер будет ждать 30 мин TTL чтобы перезапустить.
            _wizard_release(rid)
    else:
        _wizard_release(rid)


@router.callback_query(F.data.startswith("ozon_book_auto:"))
async def cb_ozon_book_auto(cb: CallbackQuery, state: FSMContext) -> None:
    """Старт wizard'а в **авто-режиме**: scoring picker пропускается, сразу Auto-walk
    для каждого оставшегося кластера. Юзер: «не понимаю почему опять смотрит scored,
    хочу автоматом». Триггерится кнопкой «🚀 Бронировать следующее направление»
    после успешного booking'а.
    """
    parts = cb.data.split(":")
    rid = int(parts[1])
    mode = parts[2] if len(parts) >= 3 else "direct"
    if not _wizard_acquire(rid):
        await cb.answer(
            f"⏳ Ozon-мастер для поставки #{rid} уже запущен. Подожди завершения.",
            show_alert=True,
        )
        return
    await cb.answer(f"Авто-бронирование ({mode.upper()})…")
    tg_id = current_user_id_from(cb)
    if tg_id is None:
        _wizard_release(rid)
        return
    if cb.message:
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        # Флаг — будет прочитан в `_show_scored_warehouse_picker` для DIRECT.
        await state.update_data(ob_auto_walk=True)
        try:
            launched = await _start_ozon_book_wizard(cb.message, state, rid, tg_id, mode=mode)
        except Exception:
            _wizard_release(rid)
            raise
        if not launched:
            _wizard_release(rid)
    else:
        _wizard_release(rid)


# ── Авто-брон на одну дату (Фаза 3 умной брони) ──────────────────────────
# План: C:\Users\vladi\.claude\plans\smart-supply-booking.md
# Сейчас — этап «explore»: создаём drafts для всех Ozon-кластеров заявки,
# собираем слоты, ищем оптимальную дату. Bulk-book — следующая итерация.


@router.callback_query(F.data.startswith("obauto:"))
async def cb_obauto(cb: CallbackQuery, state: FSMContext) -> None:
    """Авто-брон на одну дату. Варианты callback'ов:
      obauto:<rid>           — стандартный: explore + показать кнопки выбора
      obauto:hour:<rid>      — explore + сразу bulk-book на best_date + best_hour
      obauto:bday:<rid>      — handled by cb_obauto_book_day (после explore)
      obauto:bhour:<rid>     — handled by cb_obauto_book_hour
    """
    parts = cb.data.split(":")
    if len(parts) < 2:
        await cb.answer("Битый callback", show_alert=True)
        return
    # Отсекаем под-callback'и bday/bhour — у них свои handlers
    if parts[1] in ("bday", "bhour"):
        return  # обработают cb_obauto_book_day / cb_obauto_book_hour
    # auto_mode определяет что делать после explore.
    if parts[1] == "hour":
        rid = int(parts[2])
        auto_mode = "hour"
    else:
        rid = int(parts[1])
        # Из ship_plan «🎯 В одну дату» — сразу bday (callback obauto:<rid>).
        auto_mode = "day"
    tg_id = cb.from_user.id if cb.from_user else 0

    # Сохраняем выбранный режим в БД для отображения в карточке.
    try:
        with db_session() as _s:
            _r = get_shipment_request(_s, rid, user_id=tg_id)
            if _r:
                _r.auto_book_mode = auto_mode
    except Exception:
        pass

    if not _wizard_acquire(rid):
        await cb.answer(
            f"⏳ Ozon-мастер для поставки #{rid} уже запущен.",
            show_alert=True,
        )
        return
    await cb.answer(
        "🎯 Найду оптимальную дату и забронирую…"
        if auto_mode in ("day", "hour") else "🎯 Запускаю…"
    )
    if not cb.message:
        _wizard_release(rid)
        return
    try:
        await _auto_book_explore(cb.message, state, rid, tg_id, auto_mode=auto_mode)
    finally:
        _wizard_release(rid)


async def _auto_book_explore(
    msg: Message, state: FSMContext, rid: int, tg_id: int,
    *, auto_mode: str = "manual",
) -> None:
    """Создать drafts для всех Ozon-кластеров заявки, дёрнуть scoring +
    timeslot/info, посчитать `find_best_common_date`, показать юзеру.

    Не бронирует — это этап «explore», даём юзеру выбрать что делать.
    """
    from src.services.auto_book import (
        parse_timeslot_response, find_best_common_date, date_options_summary,
    )

    # 1. Контекст заявки
    with db_session() as session:
        req = get_shipment_request(session, rid, user_id=tg_id)
        if not req:
            await msg.answer(f"Поставка #{rid} не найдена.")
            return
        if not req.target_date_from:
            await msg.answer(
                "⚠ Сначала задай даты в /ship_plan — авто-брон ищет по ним."
            )
            return
        oz_clusters = sorted({
            it.cluster for it in req.items
            if it.marketplace == "ozon" and not it.booked_supply_id
        })
        date_picks = list(req.target_dates_json or [])
        hour_picks = list(req.target_hours_json or [])
        items_per_cluster: Dict[str, List[Dict]] = {}
        for cl in oz_clusters:
            items, _ = _build_items_for_cluster(req, cl)
            items_per_cluster[cl] = items
        ozon_supply_type = req.ozon_supply_type or "direct"
        crossdock_map = dict(req.crossdock_warehouses_json or {})

    if not oz_clusters:
        await msg.answer("В заявке нет несзабронированных Ozon-направлений.")
        return

    # CROSSDOCK без drop-off в БД — авто-брон не сработает (нужен warehouse_id
    # для draft_create). Перекидываем сразу в карточку поставки — юзер сам
    # кликнет обычный wizard «🚛 Создать поставку Ozon → Кросс-докинг»,
    # пройдёт drop-off picker, drop-off сохранится в БД. Никаких промежуточных
    # сообщений (юзер просил без них).
    is_cross = ozon_supply_type == "cross"
    if is_cross:
        unfilled = [cl for cl in oz_clusters if not crossdock_map.get(cl)]
        if unfilled:
            from src.bot.handlers.shipment import _render_request_card
            with db_session() as session:
                req = get_shipment_request(session, rid, user_id=tg_id)
                if req:
                    text, kb = _render_request_card(req)
                    await safe_edit_or_answer(msg, text, reply_markup=kb)
            return

    # 2. Ozon client
    with db_session() as s:
        oz = get_ozon_client_for(s, tg_id)
    if oz is None:
        await msg.answer(_NO_OZON_KEYS_MSG)
        return

    # 3. Прогресс-сообщение (одно, обновляем через edit_text)
    allowed_dates_set = set()
    from datetime import date as _date_cls
    for d in date_picks:
        try:
            allowed_dates_set.add(_date_cls.fromisoformat(d))
        except (ValueError, TypeError):
            pass
    allowed_hours_set = set(int(h) for h in hour_picks) if hour_picks else None

    progress = await msg.answer(
        f"🎯 <b>Авто-брон поставки #{rid}</b>\n\n"
        f"Кластеров: <b>{len(oz_clusters)}</b> ({', '.join(oz_clusters)})\n"
        f"Дат: {', '.join(sorted(date_picks)) if date_picks else 'все 7 дней'}\n"
        f"Часы: {sorted(allowed_hours_set) if allowed_hours_set else 'любые'}\n\n"
        f"⏳ Создаю drafts… (~30-90с/кластер из-за лимита Ozon)"
    )

    async def _edit(text: str) -> None:
        try:
            await progress.edit_text(text)
        except Exception:
            pass  # MessageNotModified

    # 4. Per-cluster: создать draft, дождаться scoring, дёрнуть timeslot/info
    slots_per_cluster: Dict[str, List] = {}
    drafts_meta: Dict[str, Dict] = {}  # cluster → {draft_id, cluster_id, wh_id, wh_name, drop_off}
    log_lines: List[str] = []
    supply_type = 1 if is_cross else 2
    draft_type = "CREATE_TYPE_CROSSDOCK" if is_cross else "CREATE_TYPE_DIRECT"

    # Дата-диапазон для timeslot/info: от today до max(date_picks) + 1д.
    from datetime import datetime as _dt, timezone, timedelta
    now = _dt.now(timezone.utc)
    date_from_iso = now.strftime("%Y-%m-%dT00:00:00Z")
    if allowed_dates_set:
        max_d = max(allowed_dates_set)
        date_to_iso = (_dt.combine(max_d, _dt.min.time()) + timedelta(days=1)).replace(
            tzinfo=timezone.utc
        ).strftime("%Y-%m-%dT23:59:59Z")
    else:
        date_to_iso = (now + timedelta(days=7)).strftime("%Y-%m-%dT23:59:59Z")

    for idx, cl in enumerate(oz_clusters, 1):
        items = items_per_cluster[cl]
        if not items:
            log_lines.append(f"  ⚠ <b>{cl}</b>: пустой состав")
            continue
        # cluster_id (macrolocal)
        cl_id = await _resolve_macrolocal_id(oz, cl)
        if not cl_id:
            log_lines.append(f"  ⚠ <b>{cl}</b>: cluster_id не сматчил")
            continue
        # CROSSDOCK требует drop_off — из ShipmentRequest.crossdock_warehouses_json
        drop_off = None
        if is_cross:
            v = crossdock_map.get(cl)
            try:
                drop_off = int(v) if v else None
            except (ValueError, TypeError):
                drop_off = None
            if not drop_off:
                log_lines.append(
                    f"  ⚠ <b>{cl}</b>: для CROSSDOCK нужен drop-off — задай через wizard"
                )
                continue

        await _edit(
            f"🎯 <b>Авто-брон поставки #{rid}</b>\n\n"
            f"⏳ Кластер {idx}/{len(oz_clusters)}: <b>{cl}</b> — создаю draft…\n\n"
            + "\n".join(log_lines)
        )

        try:
            op_id = await oz.draft_create(
                items=items,
                cluster_ids=[cl_id],
                draft_type=draft_type,
                drop_off_point_warehouse_id=drop_off,
            )
        except OzonAPIError as e:
            log_lines.append(f"  ❌ <b>{cl}</b>: draft_create — <code>{str(e)[:120]}</code>")
            continue
        if op_id.startswith("sync:"):
            draft_id = int(op_id.split(":", 1)[1])
        else:
            # async — polling до DONE
            try:
                info = await _wait_draft_ready(oz, op_id, max_attempts=10)
            except OzonAPIError as e:
                log_lines.append(f"  ❌ <b>{cl}</b>: poll — <code>{str(e)[:120]}</code>")
                continue
            draft_id = int(info.get("draft_id") or 0)
            if not draft_id:
                log_lines.append(f"  ❌ <b>{cl}</b>: draft_id не получен")
                continue

        # Wait scoring (FULL_AVAILABLE)
        wh_id = None
        wh_name = ""
        for attempt in range(5):
            await asyncio.sleep(3)
            try:
                info = await oz.draft_create_info(draft_id=draft_id)
            except OzonAPIError as e:
                if "429" in str(e):
                    await asyncio.sleep(15)
                    continue
                break
            all_whs = [
                w for c in info.get("clusters", []) for w in c.get("warehouses", [])
            ]
            avail = [
                w for w in all_whs
                if (w.get("availability_status") or {}).get("state") == "FULL_AVAILABLE"
            ]
            if avail:
                # Берём top-1 по rank
                avail.sort(key=lambda w: w.get("total_rank", 999))
                sw = avail[0].get("storage_warehouse") or {}
                wh_id = int(sw.get("warehouse_id") or 0)
                wh_name = str(sw.get("name") or "")
                break
        if not wh_id:
            log_lines.append(
                f"  ⚠ <b>{cl}</b>: scoring не нашёл подходящий склад (FULL_AVAILABLE)"
            )
            continue

        # Дёргаем timeslot/info
        try:
            ts_response = await oz.draft_timeslot_info(
                draft_id=draft_id,
                date_from=date_from_iso,
                date_to=date_to_iso,
                warehouse_ids=[wh_id],
                cluster_id=cl_id,
                supply_type=supply_type,
                retries_on_429=1,
            )
        except OzonAPIError as e:
            log_lines.append(f"  ❌ <b>{cl}</b>: timeslot — <code>{str(e)[:120]}</code>")
            continue
        slots = parse_timeslot_response(
            ts_response, cluster=cl, warehouse_id=wh_id, warehouse_name=wh_name,
        )
        slots_per_cluster[cl] = slots
        drafts_meta[cl] = {
            "draft_id": draft_id, "cluster_id": cl_id,
            "wh_id": wh_id, "wh_name": wh_name,
            "drop_off_name": "", "supply_type": supply_type,
        }
        unique_dates = sorted(set(s.date for s in slots))
        log_lines.append(
            f"  ✅ <b>{cl}</b> ({wh_name}): {len(slots)} слотов, "
            f"{len(unique_dates)} дат"
        )
        # Пауза между кластерами для соблюдения лимитов Ozon
        if idx < len(oz_clusters):
            await asyncio.sleep(5)

    # 5. Алгоритм best_date
    best_date = find_best_common_date(
        slots_per_cluster,
        allowed_dates=allowed_dates_set or None,
        allowed_hours=allowed_hours_set,
    )
    summary_lines = date_options_summary(
        slots_per_cluster,
        allowed_dates=allowed_dates_set or None,
        allowed_hours=allowed_hours_set,
    )

    # 6. Сохраняем контекст в state для callback'ов bulk-book
    # SlotInfo сериализуем как dict (state хранит JSON-совместимое).
    slots_serialized = {
        cl: [
            {"from_ts": s.from_ts, "to_ts": s.to_ts,
             "warehouse_id": s.warehouse_id, "warehouse_name": s.warehouse_name}
            for s in slots
        ]
        for cl, slots in slots_per_cluster.items()
    }
    await state.update_data(
        ob_rid=rid,
        ob_tg_id=tg_id,
        ob_auto_slots=slots_serialized,
        ob_auto_drafts=drafts_meta,
        ob_auto_best_date=best_date.isoformat() if best_date else None,
        ob_auto_allowed_hours=sorted(allowed_hours_set) if allowed_hours_set else None,
    )

    # 7. Итоговое сообщение + кнопки бронирования
    result_lines = [
        f"🎯 <b>Авто-брон поставки #{rid} — результат</b>\n",
        f"<i>Расчёты:</i>",
    ]
    result_lines.extend(log_lines)
    result_lines.append("")
    rows: List[List[InlineKeyboardButton]] = []
    if best_date is None:
        result_lines.append(
            "🔴 <b>Не нашёл общей даты</b> — нет слотов ни по одной из твоих дат.\n"
            "Расширь даты в /ship_plan и попробуй ещё раз."
        )
    else:
        clusters_on_best = sorted({
            s.cluster for slots in slots_per_cluster.values()
            for s in slots if s.date == best_date
            and (allowed_hours_set is None or s.hour in allowed_hours_set)
        })
        date_label = best_date.strftime("%d.%m")
        result_lines.append(
            f"🎯 <b>Оптимальная дата: {date_label}</b>\n"
            f"   Могут уехать: <b>{len(clusters_on_best)}/{len(oz_clusters)}</b> "
            f"({', '.join(clusters_on_best)})"
        )
        if len(summary_lines) > 1:
            result_lines.append("\n<i>Топ дат:</i>")
            for d, n, cls in summary_lines[:5]:
                result_lines.append(
                    f"  {d.strftime('%d.%m')}: {n} кл. — {', '.join(cls[:4])}"
                )
        result_lines.append(
            f"\n<i>Кластеры без слотов на {date_label} — после брони запустим "
            f"авто-поиск в течение часа.</i>"
        )
        if auto_mode == "manual":
            rows.append([InlineKeyboardButton(
                text=f"🎯 Брон {date_label} — все в один день",
                callback_data=f"obauto:bday:{rid}",
            )])
            rows.append([InlineKeyboardButton(
                text=f"🎯 Брон {date_label} + один час старта",
                callback_data=f"obauto:bhour:{rid}",
            )])
    rows.append([InlineKeyboardButton(text="✖ Отмена",
                                       callback_data=f"ship_open:{rid}")])

    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await _edit("\n".join(result_lines))
    try:
        await progress.edit_reply_markup(reply_markup=kb)
    except Exception:
        pass

    # Auto-mode: сразу запускаем bulk-book без confirm от юзера.
    if best_date is not None and auto_mode in ("day", "hour"):
        # Эмулируем cb_obauto_book_day / cb_obauto_book_hour через дополнительный
        # callback. Чтобы не дублировать код — делаем internal call с теми же
        # параметрами state как они ждут.
        await _auto_run_bulk(msg, state, rid, mode=auto_mode)


async def _auto_run_bulk(
    msg: Message, state: FSMContext, rid: int, *, mode: str,
) -> None:
    """Внутренний эквивалент cb_obauto_book_day / cb_obauto_book_hour —
    зовётся когда юзер выбрал режим ещё на шаге ship_plan и нам не нужен
    второй confirm. mode = 'day' | 'hour'.
    """
    data = await state.get_data()
    best_date_iso = data.get("ob_auto_best_date")
    slots_ser = data.get("ob_auto_slots") or {}
    drafts_meta = data.get("ob_auto_drafts") or {}
    allowed_hours = data.get("ob_auto_allowed_hours") or None
    if not best_date_iso or not slots_ser:
        return
    allowed_hours_set = set(allowed_hours) if allowed_hours else None

    choices: Dict[str, Dict] = {}
    not_fit: List[str] = []

    if mode == "day":
        for idx, (cluster, slots_list) in enumerate(slots_ser.items()):
            on_date = [
                s for s in slots_list
                if s["from_ts"][:10] == best_date_iso
                and (allowed_hours_set is None or int(s["from_ts"][11:13]) in allowed_hours_set)
            ]
            if not on_date:
                not_fit.append(cluster)
                continue
            on_date.sort(key=lambda s: s["from_ts"])
            choices[str(idx)] = _slot_dict_to_picker_choice(
                on_date[0], cluster, drafts_meta.get(cluster, {})
            )
    else:  # mode == "hour"
        hour_to_clusters: Dict[int, set] = {}
        cluster_slot_by_hour: Dict[tuple, Dict] = {}
        for cluster, slots_list in slots_ser.items():
            for s in slots_list:
                if s["from_ts"][:10] != best_date_iso:
                    continue
                hour = int(s["from_ts"][11:13])
                if allowed_hours_set is not None and hour not in allowed_hours_set:
                    continue
                hour_to_clusters.setdefault(hour, set()).add(cluster)
                if (cluster, hour) not in cluster_slot_by_hour:
                    cluster_slot_by_hour[(cluster, hour)] = s
        if not hour_to_clusters:
            await msg.answer("⚠ Не нашёл общих часов на эту дату.")
            return
        best_hour = max(hour_to_clusters.items(), key=lambda kv: (len(kv[1]), -kv[0]))[0]
        clusters_at_hour = hour_to_clusters[best_hour]
        await msg.answer(
            f"🎯 Общий час: <b>{best_hour:02d}:00</b> · "
            f"{len(clusters_at_hour)} кластеров"
        )
        for idx, cluster in enumerate(slots_ser.keys()):
            if cluster not in clusters_at_hour:
                not_fit.append(cluster)
                continue
            slot = cluster_slot_by_hour[(cluster, best_hour)]
            choices[str(idx)] = _slot_dict_to_picker_choice(
                slot, cluster, drafts_meta.get(cluster, {})
            )

    if not choices:
        await msg.answer("⚠ Не нашлось ни одного слота для авто-брон.")
        return

    await _spawn_auto_poll_for_best_date(msg, rid, best_date_iso, not_fit, drafts_meta, data)
    await state.update_data(
        ob_picker_choices=choices,
        ob_picker_clusters=[],
        ob_picker_msg_id=None,
        ob_progress_msg_id=msg.message_id,
        ob_failed_clusters=not_fit,
    )
    await _run_bulk_book(msg.bot, msg, state)


def _slot_dict_to_picker_choice(slot_dict: Dict, cluster: str, meta: Dict) -> Dict:
    """Из {from_ts, to_ts, warehouse_id, warehouse_name} + meta делаем choice
    структуру для `_run_bulk_book` (которая ждёт ob_picker_choices словарь).
    """
    return {
        "cluster": cluster,
        "cluster_id": meta.get("cluster_id"),
        "draft_id": meta.get("draft_id"),
        "supply_type": meta.get("supply_type", 2),
        "drop_off_name": meta.get("drop_off_name", ""),
        "warehouse_id": slot_dict["warehouse_id"],
        "warehouse_name": slot_dict["warehouse_name"],
        # _run_bulk_book ждёт "from"/"to" с Z-суффиксом (как Ozon шлёт).
        # У нас в state from_ts без Z (локальный) — добавим.
        "from": slot_dict["from_ts"] + ("Z" if not slot_dict["from_ts"].endswith("Z") else ""),
        "to": slot_dict["to_ts"] + ("Z" if not slot_dict["to_ts"].endswith("Z") else ""),
    }


@router.callback_query(F.data.startswith("obauto:bday:"))
async def cb_obauto_book_day(cb: CallbackQuery, state: FSMContext) -> None:
    """Bulk-book на best_date — для каждого кластера со слотами на эту дату
    берём самый ранний слот (либо в allowed_hours)."""
    if not cb.message:
        return
    rid = int(cb.data.split(":")[2])
    data = await state.get_data()
    best_date_iso = data.get("ob_auto_best_date")
    slots_ser = data.get("ob_auto_slots") or {}
    drafts_meta = data.get("ob_auto_drafts") or {}
    allowed_hours = data.get("ob_auto_allowed_hours") or None
    if not best_date_iso or not slots_ser:
        await cb.answer("Контекст потерян — запусти 🎯 Авто-брон заново", show_alert=True)
        return
    await cb.answer(f"Бронирую на {best_date_iso}…")

    from datetime import date as _date_cls
    best_d = _date_cls.fromisoformat(best_date_iso)
    allowed_hours_set = set(allowed_hours) if allowed_hours else None

    # Собираем choices: для каждого кластера — самый ранний слот на best_date
    choices: Dict[str, Dict] = {}
    not_fit_clusters: List[str] = []
    for idx, (cluster, slots_list) in enumerate(slots_ser.items()):
        on_date = [
            s for s in slots_list
            if s["from_ts"][:10] == best_date_iso
            and (allowed_hours_set is None or int(s["from_ts"][11:13]) in allowed_hours_set)
        ]
        if not on_date:
            not_fit_clusters.append(cluster)
            continue
        on_date.sort(key=lambda s: s["from_ts"])
        choices[str(idx)] = _slot_dict_to_picker_choice(
            on_date[0], cluster, drafts_meta.get(cluster, {})
        )

    if not choices:
        await cb.message.answer(
            "⚠ Не нашлось ни одного слота на эту дату. Попробуй другую."
        )
        return

    # Запускаем auto-poll для кластеров без слота на best_date.
    # Бот будет дёргать timeslot/info каждые 60с в течение часа и забронирует
    # как только слот появится на best_date.
    await _spawn_auto_poll_for_best_date(
        cb.message, rid, best_date_iso, not_fit_clusters, drafts_meta, data,
    )

    # Готовим state как ожидает _run_bulk_book
    await state.update_data(
        ob_picker_choices=choices,
        ob_picker_clusters=[],  # не критично для bulk-book
        ob_picker_msg_id=None,
        ob_progress_msg_id=cb.message.message_id,
        ob_failed_clusters=not_fit_clusters,
    )
    await _run_bulk_book(cb.message.bot, cb.message, state)


async def _spawn_auto_poll_for_best_date(
    msg: Message, rid: int, best_date_iso: str,
    failed_clusters: List[str], drafts_meta: Dict[str, Dict],
    state_data: Dict,
) -> None:
    """Запустить background auto-poll для кластеров где нет слотов на best_date.
    Использует существующий `_auto_poll_slots` с фильтром date_picks=[best_date]."""
    import time as _time
    if not failed_clusters:
        return
    poll_drafts: List[Dict] = []
    crossdock_map = state_data.get("ob_dropoff_choices") or {}
    is_cross = (state_data.get("ob_type") or "").upper().endswith("CROSSDOCK")
    draft_type = "CREATE_TYPE_CROSSDOCK" if is_cross else "CREATE_TYPE_DIRECT"
    for cl in failed_clusters:
        meta = drafts_meta.get(cl)
        if not meta:
            continue
        do_choice = crossdock_map.get(cl) or {}
        poll_drafts.append({
            "cluster": cl,
            "cluster_id": int(meta.get("cluster_id") or 0),
            "draft_id": int(meta.get("draft_id") or 0),
            "drop_off_warehouse_id": int(do_choice.get("wh_id") or 0) if is_cross else None,
            "drop_off_warehouse_name": do_choice.get("name") if is_cross else None,
            "draft_type": draft_type,
            "created_ts": _time.time(),
            "wh_id": int(meta.get("wh_id") or 0),
            "supply_type": int(meta.get("supply_type") or 2),
        })
    if not poll_drafts:
        return
    # date_picks=[best_date] — auto-poll будет фильтровать слоты по этой дате.
    date_from_iso = f"{best_date_iso}T00:00:00Z"
    date_to_iso = f"{best_date_iso}T23:59:59Z"
    task = asyncio.create_task(_auto_poll_slots(
        msg.bot, msg.chat.id, rid, poll_drafts,
        date_from_iso, date_to_iso,
        [best_date_iso],
        state_data.get("ob_hour_picks") or [],
        tg_id=state_data.get("ob_tg_id"),
        auto_book=True,  # из 🎯 авто-брона — бронируем найденное автомат
    ))
    _AUTO_POLL_TASKS[rid] = task
    try:
        await msg.bot.send_message(
            msg.chat.id,
            f"♻ <b>Авто-поиск запущен</b> на {best_date_iso}\n"
            f"Кластеры: {', '.join(failed_clusters)}\n"
            f"<i>Бот будет проверять слоты каждые 60с в течение часа. "
            f"Если найдётся — забронирует и пришлёт сообщение.</i>"
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("obauto:bhour:"))
async def cb_obauto_book_hour(cb: CallbackQuery, state: FSMContext) -> None:
    """Bulk-book на best_date + один час старта — находим час с максимумом
    пересекающихся кластеров, берём слот этого часа в каждом кластере."""
    if not cb.message:
        return
    rid = int(cb.data.split(":")[2])
    data = await state.get_data()
    best_date_iso = data.get("ob_auto_best_date")
    slots_ser = data.get("ob_auto_slots") or {}
    drafts_meta = data.get("ob_auto_drafts") or {}
    allowed_hours = data.get("ob_auto_allowed_hours") or None
    if not best_date_iso or not slots_ser:
        await cb.answer("Контекст потерян — запусти 🎯 Авто-брон заново", show_alert=True)
        return
    await cb.answer("Ищу общий час…")

    allowed_hours_set = set(allowed_hours) if allowed_hours else None

    # Считаем для каждого часа: какие кластеры имеют слот.
    hour_to_clusters: Dict[int, Set[str]] = {}
    cluster_slot_by_hour: Dict[tuple, Dict] = {}  # (cluster, hour) → slot_dict
    for cluster, slots_list in slots_ser.items():
        for s in slots_list:
            if s["from_ts"][:10] != best_date_iso:
                continue
            hour = int(s["from_ts"][11:13])
            if allowed_hours_set is not None and hour not in allowed_hours_set:
                continue
            hour_to_clusters.setdefault(hour, set()).add(cluster)
            if (cluster, hour) not in cluster_slot_by_hour:
                cluster_slot_by_hour[(cluster, hour)] = s

    if not hour_to_clusters:
        await cb.message.answer("⚠ Не нашлось общих часов на эту дату.")
        return

    # Лучший час = max-кластеров (tie-break: пораньше).
    best_hour = max(hour_to_clusters.items(), key=lambda kv: (len(kv[1]), -kv[0]))[0]
    clusters_at_hour = hour_to_clusters[best_hour]

    choices: Dict[str, Dict] = {}
    not_fit_clusters: List[str] = []
    for idx, cluster in enumerate(slots_ser.keys()):
        if cluster not in clusters_at_hour:
            not_fit_clusters.append(cluster)
            continue
        slot = cluster_slot_by_hour[(cluster, best_hour)]
        choices[str(idx)] = _slot_dict_to_picker_choice(
            slot, cluster, drafts_meta.get(cluster, {})
        )

    if not choices:
        await cb.message.answer("⚠ Не нашлось слотов на общий час.")
        return

    await cb.message.answer(
        f"🎯 Общий час: <b>{best_hour:02d}:00</b> · "
        f"{len(clusters_at_hour)} кластеров"
    )

    # Auto-poll для кластеров без слота на этот час на best_date
    await _spawn_auto_poll_for_best_date(
        cb.message, rid, best_date_iso, not_fit_clusters, drafts_meta, data,
    )

    await state.update_data(
        ob_picker_choices=choices,
        ob_picker_clusters=[],
        ob_picker_msg_id=None,
        ob_progress_msg_id=cb.message.message_id,
        ob_failed_clusters=not_fit_clusters,
    )
    await _run_bulk_book(cb.message.bot, cb.message, state)


async def _resolve_macrolocal_id(oz: OzonClient, cluster_name: str) -> Optional[int]:
    """Поиск macrolocal_cluster_id по имени кластера."""
    try:
        clusters = await oz.cluster_list(allow_stale=True)
    except OzonAPIError:
        return None
    target_norm = _normalize(cluster_name)
    for cl in clusters:
        if _normalize(cl.get("name") or "") == target_norm:
            mcid = cl.get("macrolocal_cluster_id") or cl.get("id")
            try:
                return int(mcid)
            except (ValueError, TypeError):
                return None
    # Soft-match по подстроке
    for cl in clusters:
        if target_norm in _normalize(cl.get("name") or ""):
            mcid = cl.get("macrolocal_cluster_id") or cl.get("id")
            try:
                return int(mcid)
            except (ValueError, TypeError):
                return None
    return None


# ── Конец авто-брон секции ──────────────────────────────────────────────


async def _start_ozon_book_wizard(
    msg: Message, state: FSMContext, rid: int, tg_id: int, *, mode: str = "direct",
) -> bool:
    """Возвращает True если wizard взлетел в multi-step FSM-режим, False — если
    был early-exit (нет ключей/нет дат/всё уже забронировано). Caller использует
    результат чтобы решить — снимать ли lock сразу.

    tg_id — Telegram-ID юзера, чьи Ozon-креды используются (multi-tenant).
    Сохраняется в state как ob_tg_id и читается всеми последующими шагами через
    `_ozon_client_from_state`.
    """
    with db_session() as s:
        creds = get_ozon_creds(s, tg_id)
    if creds is None:
        await msg.answer(_NO_OZON_KEYS_MSG)
        return False

    # Собираем Ozon-направления заявки
    summaries: List[Tuple[str, int, int, List[str]]] = []  # (cluster, n_items, total_qty, missing)
    with db_session() as session:
        req = get_shipment_request(session, rid, user_id=tg_id)
        if not req:
            await msg.answer(f"Поставка #{rid} не найдена.")
            return False
        if not req.target_date_from:
            # Не команда «Сначала /ship_plan» — а сразу инлайн-кнопка
            # «📅 Запланировать даты» (юзер не должен видеть командный синтаксис).
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="📅 Запланировать даты",
                    callback_data=f"ship_plan:{rid}",
                )],
                [InlineKeyboardButton(
                    text="◀ К карточке заявки",
                    callback_data=f"ship_open:{rid}",
                )],
            ])
            await safe_edit_or_answer(
                msg,
                f"⚠ У заявки #{rid} ещё нет целевых дат — выбери даты "
                f"и потом возвращайся к созданию поставки.",
                reply_markup=kb,
            )
            return False

        all_oz_clusters = sorted({it.cluster for it in req.items if it.marketplace == "ozon"})
        # Кластер уже забронирован, если у ЛЮБОГО его item проставлен booked_supply_id.
        booked_clusters = sorted({
            it.cluster for it in req.items
            if it.marketplace == "ozon" and it.booked_supply_id
        })
        oz_clusters = [c for c in all_oz_clusters if c not in booked_clusters]
        for cl in oz_clusters:
            items, missing = _build_items_for_cluster(req, cl)
            total_qty = sum(it["quantity"] for it in items)
            summaries.append((cl, len(items), total_qty, missing))
        date_from = req.target_date_from.date().isoformat()
        date_to = (req.target_date_to or req.target_date_from).date().isoformat()
        # Список конкретно выбранных дат (для фильтрации слотов на нашей стороне:
        # Ozon-API принимает только диапазон date_from..date_to и возвращает все
        # дни между ними, включая невыбранные).
        date_picks = list(req.target_dates_json or [])
        # Часы суток. NULL/пусто = «любое время» (без фильтра). Иначе слоты, час
        # старта которых не в списке, отбрасываем после получения от Ozon.
        hour_picks = list(req.target_hours_json or [])

    if not summaries:
        if booked_clusters:
            await msg.answer(
                f"✅ Все Ozon-кластеры заявки #{rid} уже забронированы:\n"
                + "\n".join(f"  • {c}" for c in booked_clusters)
            )
        else:
            await msg.answer(f"В заявке #{rid} нет Ozon-направлений.")
        return False

    # Если уже в авто-режиме (юзер тапнул «🚀 Бронировать следующее» после
    # успешного booking'а предыдущего направления) — info card не нужна, юзер
    # эту инфу видел только что. Просто компактно «🔄 авто-режим: следующее».
    state_data_pre = await state.get_data()
    is_auto = bool(state_data_pre.get("ob_auto_walk"))

    has_missing = any(missing for _, _, _, missing in summaries)
    # Реально-выбранные даты, а не диапазон from..to (могут не совпадать если
    # юзер выбрал не подряд — например [20, 23] вместо [20, 21, 22, 23]).
    from src.bot.helpers import format_picked_dates
    dates_label = format_picked_dates(date_picks, fallback_from=date_from, fallback_to=date_to)

    # mode: "direct" → Прямая поставка (везти на РФЦ); "cross" → Кросс-докинг (везти в хаб)
    ob_type = "CREATE_TYPE_CROSSDOCK" if mode == "cross" else "CREATE_TYPE_DIRECT"
    type_label = "Кросс-докинг 🚛" if mode == "cross" else "Прямая 🚀"

    if is_auto:
        # Компактный заголовок — без полного info card, без даты/режима/SKU-списков.
        remaining = ", ".join(s[0] for s in summaries)
        lines = [
            f"🔄 <b>Авто-бронирование оставшихся ({len(summaries)})</b>: {remaining}"
        ]
    else:
        lines = [f"📦 <b>Создание Ozon-поставок для заявки #{rid}</b>\n"]
        if booked_clusters:
            lines.append(
                f"✅ Уже забронированы ({len(booked_clusters)}): "
                + ", ".join(booked_clusters) + "\n"
                "Создаю поставки для оставшихся:\n"
            )
        for cl, n_items, total_qty, missing in summaries:
            lines.append(f"<b>«{cl}»</b>: {n_items} SKU, {total_qty} шт")
            if missing:
                lines.append(f"  ⚠ Без offer_id ({len(missing)}): {', '.join(missing[:5])}")
        lines.append(f"\nДаты: {dates_label}")
        if has_missing:
            lines.append(
                "\n💡 Запусти /sku_link_ozon чтобы привязать недостающие SKU к Ozon offer_id + sku."
            )
    await state.update_data(
        ob_rid=rid,
        ob_tg_id=tg_id,
        ob_clusters=[s[0] for s in summaries],
        ob_date_from=date_from,
        ob_date_to=date_to,
        ob_date_picks=date_picks,
        ob_hour_picks=hour_picks,
        ob_type=ob_type,
        ob_wh_choices={},
        ob_cluster_idx=0,
        ob_dropoff_choices={},  # cluster_name → {wh_id, name}
    )
    if not is_auto:
        lines.append(f"\n📦 Режим: <b>{type_label}</b>")
    # Начинаем новую «сардельку» с info card (в auto-режиме — компактным заголовком).
    # Drop-off-picker и slot-picker остаются отдельными (нужны inline-кнопки), но
    # всё остальное (info → drop-off-confirm → создание драфтов → scoring) попадает
    # в одно сообщение.
    await progress_reset(state)
    await progress_start(msg, state, "\n".join(lines))
    if mode == "cross":
        from src.services.draft_cache import get_dropoff_choices_for_request
        with db_session() as session:
            cached_choices = get_dropoff_choices_for_request(session, rid)
        oz_clusters = [s[0] for s in summaries]
        if oz_clusters and all(c in cached_choices for c in oz_clusters):
            await state.update_data(ob_dropoff_choices=cached_choices)
            await progress_add(
                msg, state,
                "\n♻ Drop-off-точки восстановлены из прошлого выбора:\n"
                + "\n".join(
                    f"  • <b>{cached_choices[c]['name']}</b> → «{c}»"
                    for c in oz_clusters
                )
            )
            await _create_drafts_and_fetch_scoring(msg, state)
        else:
            await _ask_dropoff_for_next_cluster(msg, state)
    else:
        await _create_drafts_and_fetch_scoring(msg, state)
    return True


async def _invalidate_failed_draft(draft_id: int) -> None:
    """Удалить cached draft если scoring провалился — чтобы при повторном
    входе бот не подтянул его и выбранный drop-off-хаб заново."""
    from src.db.models import OzonDraftCache
    try:
        with db_session() as session:
            row = session.query(OzonDraftCache).filter(
                OzonDraftCache.draft_id == draft_id
            ).first()
            if row:
                session.delete(row)
    except Exception as e:
        logger.warning("invalidate_failed_draft failed: %s", e)


async def _fetch_scoring_persistent(
    oz: OzonClient, draft_id: int, msg: Message,
    state: Optional[FSMContext] = None,
) -> Tuple[List[Dict], Optional[str]]:
    """Тянем draft/create/info до scored-результата.

    Возвращает `(wh_list, fail_reason)`:
      • fail_reason=None — успех (wh_list заполнен) или нормальная пустота
        для CROSSDOCK (Ozon развозит сам, конкретные РФЦ не возвращает)
        или транзиентная заминка (cooldown / timeout — можно ретрайнуть).
      • fail_reason — короткий код фатального отказа scoring'а Ozon:
          "NO_TIMESLOTS"     — у drop-off-хаба нет таймслотов в кластер
          "INVALID_ROUTE"    — маршрут из хаба в кластер не обслуживается
          "OUT_OF_ASSORTMENT"— товар не в ассортименте кластера
          "OTHER"            — иная причина FAILED (см. сообщение выше).
        При фатальном отказе ретраи бесполезны, draft из cache уже инвалидируется.

    Если передан state — статус идёт в накопительный progress-message,
    иначе как новые сообщения (legacy).

    «Спокойный режим»: 4 попытки × 60-90 сек с jitter ≈ 4-6 мин окно."""
    async def _say(line: str) -> None:
        if state is not None:
            await progress_add(msg, state, line)
        else:
            await msg.answer(line)
    import random
    max_outer = 4
    base_delay = 60  # сек, потом +jitter 0-30с
    for attempt in range(max_outer):
        wh_list: List[Dict] = []
        try:
            info = await oz.draft_create_info(draft_id=draft_id)
        except OzonAPIError as e:
            err = str(e)
            if "Cooldown" in err or "anti-abuse" in err.lower():
                await _say(
                    f"  🚫 Ozon scoring/create-info в anti-abuse cooldown.\n"
                    f"     <code>{err[:300]}</code>\n"
                    f"     Не ретраю — продлит бан."
                )
                return [], None
            if attempt + 1 < max_outer:
                delay = base_delay + random.randint(0, 30)
                await _say(
                    f"  ⏳ Попытка {attempt+1}/{max_outer}: <i>{err[:120]}</i>. "
                    f"Жду {delay}с до попытки {attempt+2}/{max_outer}…"
                )
                await asyncio.sleep(delay)
                continue
            await _say(
                f"  ❌ <b>Не удалось получить расчёт за {max_outer} попыток "
                f"(~{max_outer*(base_delay+15)//60} мин)</b>.\n"
                f"     Последняя ошибка: <code>{err[:200]}</code>\n"
                f"     🔄 Перехожу к следующему кластеру."
            )
            return [], None

        clusters_info = info.get("clusters") or []
        status = (clusters_info[0] if clusters_info else {}).get("status") or info.get("status")
        status_upper_top = str(status or "").upper()
        # FAILED + errors[].items_validation — фатальный отказ Ozon (товар
        # не в ассортименте кластера, и т.п.). Ретраить бесполезно — это
        # серверная политика, а не «scoring ещё считается».
        errors = info.get("errors") or []
        if status_upper_top == "FAILED" and errors:
            lines: List[str] = []
            # Собираем все error_reasons чтобы выбрать правильную подсказку
            all_reasons: set = set()
            for err in errors[:3]:
                err_msg = err.get("error_message") or err.get("message") or "?"
                validations = err.get("items_validation") or []
                if validations:
                    for v in validations[:5]:
                        for ri in (v.get("rejected_items") or [])[:5]:
                            reasons = ri.get("reasons") or []
                            for r in reasons:
                                all_reasons.add(str(r).upper())
                            lines.append(
                                f"   SKU <code>{ri.get('sku')}</code> в кластере "
                                f"{v.get('macrolocal_cluster_id')}: {', '.join(reasons)}"
                            )
                else:
                    reasons = err.get("error_reasons") or []
                    for r in reasons:
                        all_reasons.add(str(r).upper())
                    # error_message может быть сам по себе важной причиной
                    if err_msg and err_msg != "?":
                        all_reasons.add(err_msg.upper())
                    lines.append(f"   {err_msg}: {', '.join(reasons) if reasons else ''}")
            detail = "\n".join(lines) if lines else "(детали Ozon не вернул)"

            # Подсказка + код причины (код уходит наверх — caller знает
            # «фатально, на этом draft можно ставить крест»).
            if any("DROP_OFF_POINT_HAS_NO_TIMESLOTS" in r or "NO_TIMESLOTS" in r for r in all_reasons):
                fail_reason = "NO_TIMESLOTS"
                hint = (
                    "<i>У выбранной drop-off-точки нет таймслотов для этого кластера "
                    "на твои даты. Попробуй: (1) расширить диапазон дат, (2) выбрать "
                    "другой drop-off хаб (через ⭐ Точки кроссдока).</i>"
                )
            elif any("OUT_OF_ASSORTMENT" in r for r in all_reasons):
                fail_reason = "OUT_OF_ASSORTMENT"
                hint = (
                    "<i>Товар не в ассортименте кластера для FBO. Проверь карточку "
                    "товара в Seller Center: «Доступность по кластерам» / «Регионы».</i>"
                )
            elif any("INVALID_ROUTE" in r for r in all_reasons):
                fail_reason = "INVALID_ROUTE"
                hint = (
                    "<i>Из выбранной drop-off-точки маршрут до этого кластера не "
                    "обслуживается Ozon. Попробуй другой хаб.</i>"
                )
            else:
                fail_reason = "OTHER"
                hint = (
                    "<i>Ozon отклонил scoring. См. конкретную причину выше.</i>"
                )
            await _say(
                f"  🚫 <b>Ozon отказал в расчёте</b> (нельзя забронировать).\n{detail}\n\n{hint}"
            )
            # Удаляем cached draft чтобы при повторном входе тот же drop-off
            # не подтянулся заново и не зациклил ту же ошибку.
            if fail_reason in ("NO_TIMESLOTS", "INVALID_ROUTE"):
                await _invalidate_failed_draft(draft_id)
            return [], fail_reason
        n_unspecified = 0
        for c in clusters_info:
            for w in (c.get("warehouses") or []):
                wh_obj = (
                    w.get("storage_warehouse")
                    or w.get("supply_warehouse")
                    or {}
                )
                wh_id = w.get("warehouse_id") or wh_obj.get("warehouse_id")
                if not wh_id:
                    continue
                name = w.get("name") or wh_obj.get("name") or f"#{wh_id}"
                st = w.get("status") or w.get("availability_status") or {}
                wh_state = str(st.get("state") or "").upper()
                invalid_reason = str(st.get("invalid_reason") or "").upper()
                is_available = st.get("is_available")
                available_states = {"FULL_AVAILABLE", "PARTIAL_AVAILABLE", "AVAILABLE", "SUCCESS"}
                if is_available is None:
                    is_available = wh_state in available_states
                pending = wh_state == "UNSPECIFIED"
                if pending:
                    n_unspecified += 1
                wh_list.append({
                    "wh_id": int(wh_id),
                    "name": name,
                    "score": w.get("total_score", 0),
                    "rank": w.get("total_rank", 0),
                    "available": bool(is_available),
                    "pending": pending,
                    "reason": invalid_reason if invalid_reason and invalid_reason != "UNSPECIFIED" else wh_state,
                })
        wh_list.sort(key=lambda x: (not x["available"], x.get("rank") or 999, -x.get("score", 0)))
        n_avail = sum(1 for w in wh_list if w["available"])
        logger.info(
            "Ozon scoring draft=%s: total=%d avail=%d pending=%d cluster_status=%s",
            draft_id, len(wh_list), n_avail, n_unspecified, status,
        )

        # Scoring всё ещё считается → ждём и ретраим:
        # - либо общий status=IN_PROGRESS,
        # - либо есть склады с UNSPECIFIED статусом (Ozon ещё не закрыл их scoring).
        # ВАЖНО: для CROSSDOCK Ozon возвращает status=SUCCESS + warehouses с
        # storage_warehouse=null (РФЦ назначения определяется потом, Ozon развозит сам).
        # В таком случае wh_list пустой — но это НЕ pending, scoring готов.
        status_upper = str(status or "").upper()
        terminal_ok = status_upper in {"SUCCESS", "DONE", "CALCULATION_STATUS_DONE"}
        scoring_in_progress = (
            status_upper in {"CALCULATION_STATUS_IN_PROGRESS", "IN_PROGRESS"}
            or (not wh_list and not terminal_ok)
            or (n_avail == 0 and n_unspecified > 0)
        )
        if scoring_in_progress:
            if attempt + 1 < max_outer:
                delay = base_delay + random.randint(0, 30)
                hint = (
                    f"{n_unspecified} склад(ов) ещё считается"
                    if n_unspecified > 0 else "Ozon ещё не закончил"
                )
                await _say(
                    f"  ⏳ Попытка {attempt+1}/{max_outer}: {hint}. "
                    f"Жду {delay}с до попытки {attempt+2}/{max_outer}…"
                )
                await asyncio.sleep(delay)
                continue
            await _say(
                f"  ❌ <b>Ozon не успел посчитать за {max_outer} попыток "
                f"(~{max_outer*(base_delay+15)//60} мин)</b>.\n"
                f"     🔄 Перехожу к следующему кластеру."
            )
            return [], None

        # Для CROSSDOCK wh_list пустой — не пишем «0 складов», это путает.
        if wh_list:
            await _say(f"  ✅ Scored: {len(wh_list)} складов, доступно {n_avail}")
        return wh_list, None
    return wh_list, None


async def _create_drafts_and_fetch_scoring(msg: Message, state: FSMContext) -> None:
    """Создать draft для каждого кластера + получить scored склады через draft/create/info.
    Сохраняет drafts + scored_warehouses в state, потом показывает picker."""
    data = await state.get_data()
    rid = data.get("ob_rid")
    if rid is None:
        # State потерян — был callback из старого сообщения после /start или
        # clear'а wizard'а. Раньше падало KeyError'ом, теперь мягко выходим.
        logger.warning("_create_drafts_and_fetch_scoring: ob_rid is None — stale callback")
        await msg.answer(
            "⚠ Состояние мастера потеряно. Открой карточку заявки и тапни "
            "«🚀 Создать поставку Ozon» заново."
        )
        return

    # Single-flight: callback'и drop-off picker'а (пагинация хабов, «◀ К выбору»,
    # повторный «✓ применить ко всем») могут переоткрыть `_ask_dropoff_for_next_cluster`
    # при `idx>=len(clusters)`, что повторно вызывает эту функцию ПАРАЛЛЕЛЬНО первому
    # потоку. До lock'а это создавало дубликаты drafts в Ozon (тратилось rate-limit
    # 2 req/sec → 429 у последних кластеров). Wizard-lock тут не помогает — он
    # держится снаружи, на уровне всего wizard'а.
    if not _drafts_creating_acquire(rid):
        logger.warning("draft-creation re-entry blocked for rid=%s", rid)
        # Молча выходим — основной поток уже что-то рисует в «сардельке».
        # Юзеру кричать незачем (он скорее всего просто тыкает «вперёд/назад»).
        return

    try:
        await _create_drafts_and_fetch_scoring_inner(msg, state)
    finally:
        _drafts_creating_release(rid)


async def _create_drafts_and_fetch_scoring_inner(msg: Message, state: FSMContext) -> None:
    """Тело — см. wrapper выше. Вынесено, чтобы лочить вход без переписывания
    всех early-return'ов."""
    data = await state.get_data()
    rid = data["ob_rid"]
    clusters = data["ob_clusters"]
    draft_type = data["ob_type"]
    # Ozon enum: 2=DIRECT (точно), CROSSDOCK = 1 (Ozon принимает но раньше отвергал
    # storage_warehouse_id, теперь шлём drop_off_warehouse_id)
    supply_type = 1 if "CROSSDOCK" in (draft_type or "").upper() else 2

    oz = await _ozon_client_from_state(state)
    if oz is None:
        await msg.answer(_NO_OZON_KEYS_MSG)
        return
    drafts_made: List[Dict] = []
    scored_by_cluster: Dict[str, List[Dict]] = {}  # cluster → [{wh_id, name, score, available, reason}]
    # Кластеры, которые Ozon scoring отбил фатально (NO_TIMESLOTS / OUT_OF_ASSORTMENT / …).
    # Их draft уже инвалидирован — больше не дергаем ни scoring, ни timeslot/info.
    failed_scoring: List[Dict] = []  # [{cluster, reason}]

    # Pre-check: проверяем, что все ozon_sku из заявки реально есть в текущем
    # Ozon-кабинете. Без этого мы рискуем получить OUT_OF_ASSORTMENT (если
    # ozon_sku из другого кабинета) или, что хуже, успешно отправить мусор.
    all_skus_to_check: List[int] = []
    tg_id_pre = int(data.get("ob_tg_id") or 0)
    with db_session() as session:
        req = get_shipment_request(session, rid, user_id=tg_id_pre)
        if req:
            for cl in clusters:
                items_check, _ = _build_items_for_cluster(req, cl)
                for it in items_check:
                    if it["sku"] not in all_skus_to_check:
                        all_skus_to_check.append(it["sku"])
    # Продолжаем «сардельку» начатую в _start_ozon_book_wizard. Если её нет
    # (например прямой вход через /ozon_book) — progress_start создаст новую.
    await progress_start(msg, state, "\n⚙ <b>Создаю поставку Ozon…</b>")

    if all_skus_to_check:
        await progress_add(msg, state, "🔍 Сверяю SKU с актуальным Ozon-кабинетом…")
        missing, valid_map = await _validate_skus_in_current_account(oz, all_skus_to_check)
        if missing:
            # Подтащим article+offer_id для понятного сообщения
            lines: List[str] = []
            with db_session() as session:
                from src.db.models import OzonProduct
                bad = session.query(OzonProduct).filter(OzonProduct.sku.in_(missing)).all()
                seen_sku = set()
                for p in bad:
                    seen_sku.add(p.sku)
                    lines.append(
                        f"  • <code>{p.offer_id}</code> (sku=<code>{p.sku}</code>)"
                    )
                for s in missing:
                    if s not in seen_sku:
                        lines.append(f"  • sku=<code>{s}</code> (нет в нашей БД)")
            await msg.answer(
                f"🚫 <b>Стоп — артикулы не из текущего кабинета.</b>\n\n"
                f"В твоём Ozon-кабинете нет таких SKU:\n"
                + "\n".join(lines[:15])
                + ("\n  …" if len(lines) > 15 else "")
                + "\n\nОзон ответил бы <code>OUT_OF_ASSORTMENT</code> и заявка бы не прошла.\n"
                "<i>Открой меню → 🔗 Привязать каталог → или </i><code>/sku_link_ozon</code><i> "
                "чтобы пересинхронизировать SKU.</i>"
            )
            await state.clear()
            return

    from src.services.draft_cache import get_fresh_draft, save_draft, cleanup_expired
    # Подчищаем просроченные драфты в кэше — раз в заход достаточно.
    with db_session() as session:
        cleanup_expired(session)

    for cl_idx, cl in enumerate(clusters):
        # Пауза 8с между кластерами — Ozon /v2/draft/create/info имеет
        # per-second лимит на кабинет. Если два кластера дают scoring запрос
        # с разницей <1с → 429 «request rate limit per second».
        if cl_idx > 0:
            await asyncio.sleep(8.0)
        await progress_add(msg, state, f"🔄 Кластер <b>«{cl}»</b>…")

        # 1. Проверяем кэш — есть ли свежий draft (<25 мин) для (rid, cl)
        with db_session() as session:
            cached = get_fresh_draft(session, rid, cl)
        if cached:
            await progress_add(
                msg, state,
                f"  ♻ Переиспользую draft <code>{cached['draft_id']}</code> "
                f"(возраст {cached['age_sec']}с)."
            )
            is_cross = "CROSSDOCK" in (draft_type or "").upper()
            if is_cross:
                # CROSSDOCK: scoring всегда даёт wh_list=[] (Ozon сам выбирает РФЦ).
                # Re-fetch не нужен → не палим лимит 2/sec для повторных заходов.
                # Cached drafts с фатальным scoring-fail уже удалены из БД,
                # так что cached == «scoring был ок».
                wh_list: List[Dict] = []
                fail_reason: Optional[str] = None
            else:
                # DIRECT: scoring результат нужен для picker'а РФЦ. Re-fetch.
                wh_list, fail_reason = await _fetch_scoring_persistent(
                    oz, cached["draft_id"], msg, state=state,
                )
            if fail_reason:
                # Сохраняем всё что понадобится auto-poll'у для пересоздания draft'а
                # (cluster_id, supply_type, drop-off). Auto-poll стартует ниже если
                # причина транзиентная (NO_TIMESLOTS/INVALID_ROUTE).
                dropoff_choices = data.get("ob_dropoff_choices") or {}
                _choice = dropoff_choices.get(cl) or {}
                failed_scoring.append({
                    "cluster": cl,
                    "reason": fail_reason,
                    "cluster_id": cached["cluster_id"],
                    "supply_type": cached["supply_type"],
                    "drop_off_warehouse_id": _choice.get("wh_id"),
                    "drop_off_warehouse_name": _choice.get("name"),
                })
                continue
            drafts_made.append({
                "cluster": cl,
                "cluster_id": cached["cluster_id"],
                "draft_id": cached["draft_id"],
                "supply_type": cached["supply_type"],
            })
            scored_by_cluster[cl] = wh_list
            continue

        # 2. Свежего нет — создаём новый
        try:
            cid = await _resolve_ozon_cluster_id(oz, cl)
        except OzonAPIError as e:
            await progress_add(msg, state, f"⚠ cluster_list: <code>{str(e)[:200]}</code>")
            continue
        if not cid:
            await progress_add(msg, state, f"  ⚠ Не сматчил «{cl}» с Ozon-кластером. Пропускаю.")
            continue

        with db_session() as session:
            req = get_shipment_request(session, rid, user_id=tg_id_pre)
            if not req:
                await progress_add(msg, state, f"Заявка #{rid} пропала.")
                return
            items, _ = _build_items_for_cluster(req, cl)
        if not items:
            await progress_add(msg, state, f"  ⚠ «{cl}»: нет SKU с offer_id — нечего бронировать.")
            continue

        endpoint_label = "/v1/draft/crossdock/create" if supply_type == 1 else "/v1/draft/direct/create"
        await progress_add(
            msg, state,
            f"  POST {endpoint_label}: cluster_id={cid}, items={len(items)} (жду 15с)"
        )
        await asyncio.sleep(15.0)
        # Для CROSSDOCK: достаём drop-off-точку, выбранную ранее юзером для этого кластера
        dropoff_choices = data.get("ob_dropoff_choices") or {}
        drop_off_wh = None
        drop_off_name = None
        if "CROSSDOCK" in (draft_type or "").upper():
            choice = dropoff_choices.get(cl)
            if not choice:
                await msg.answer(
                    f"⚠ Для CROSSDOCK не выбрана drop-off-точка кластера «{cl}». "
                    "Открой /ozon_book заново и выбери точку."
                )
                continue
            drop_off_wh = int(choice.get("wh_id"))
            drop_off_name = choice.get("name")
        try:
            op_id = await oz.draft_create(
                items=items, cluster_ids=[cid], draft_type=draft_type,
                drop_off_point_warehouse_id=drop_off_wh,
            )
        except OzonAPIError as e:
            await msg.answer(f"❌ draft_create: <code>{str(e)[:400]}</code>")
            continue

        if op_id.startswith("sync:"):
            draft_id = int(op_id.split(":", 1)[1])
        else:
            await msg.answer(f"  ⏳ operation_id={op_id[:24]}…, polling…")
            info = await _wait_draft_ready(oz, op_id)
            draft_id = int(info.get("draft_id") or info.get("calculation_id") or 0)
        if not draft_id:
            await msg.answer("⚠ Нет draft_id в ответе.")
            continue

        # Сохраняем в кэш для переиспользования (если scoring сейчас отобьёт
        # NO_TIMESLOTS/INVALID_ROUTE — `_fetch_scoring_persistent` инвалидирует
        # эту запись сам, так что мы не зациклим тот же drop-off при ретрае).
        with db_session() as session:
            save_draft(session, rid, cl, cid, draft_id, supply_type,
                       drop_off_warehouse_id=drop_off_wh,
                       drop_off_warehouse_name=drop_off_name)

        await progress_add(
            msg, state,
            f"  ✅ Расчёт #{draft_id} создан, считаю варианты складов…"
        )

        # Получаем scored. До 3 минут ретраев.
        # Если scoring пуст — НЕ делаем blind-pick (это создавало 404
        # на timeslot/info и продлевало anti-abuse бан).
        # Если scoring отбил FAILED — не пихаем дохлый draft в drafts_made,
        # иначе timeslot/info будет долбить его впустую + auto-poll стартанёт.
        wh_list, fail_reason = await _fetch_scoring_persistent(
            oz, draft_id, msg, state=state,
        )
        if fail_reason:
            failed_scoring.append({
                "cluster": cl,
                "reason": fail_reason,
                "cluster_id": cid,
                "supply_type": supply_type,
                "drop_off_warehouse_id": drop_off_wh,
                "drop_off_warehouse_name": drop_off_name,
            })
            continue
        drafts_made.append({
            "cluster": cl, "cluster_id": cid, "draft_id": draft_id,
            "supply_type": supply_type,
        })
        scored_by_cluster[cl] = wh_list

    if not drafts_made:
        if failed_scoring:
            # Ozon scoring отбил все кластеры — per-кластер причины уже в «сардельке»
            # выше, тут показываем сгруппированный итог + кнопки действий.
            await _show_all_failed_scoring_summary(msg, state, failed_scoring)
        else:
            await progress_add(msg, state, "⚠ Ни один draft не создан.")
        await _release_wizard_for_state(state)
        await state.clear()
        return

    await state.update_data(
        ob_drafts=drafts_made,
        ob_scored=scored_by_cluster,
        ob_date_from_iso=f"{data['ob_date_from']}T00:00:00Z",
        ob_date_to_iso=f"{data['ob_date_to']}T23:59:59Z",
        # Имена кластеров отбитых scoring'ом — пригодятся в overview-экране,
        # чтобы пользователь видел их с пометкой «нужен другой drop-off».
        ob_failed_clusters_scoring=[fs["cluster"] for fs in failed_scoring],
    )
    await _show_scored_warehouse_picker(msg, state)


async def _show_all_failed_scoring_summary(
    msg: Message, state: FSMContext, failed_scoring: List[Dict],
) -> None:
    """Итоговый экран когда ВСЕ кластеры отбиты Ozon scoring'ом.

    Per-кластер причина уже выведена `_fetch_scoring_persistent`-ом в «сардельку»;
    здесь — сгруппированный итог + действия. Авто-поиск НЕ запускаем (бесполезно:
    Ozon не «передумает» по NO_TIMESLOTS/INVALID_ROUTE — это политика scoring'а,
    а не rate-limit)."""
    data = await state.get_data()
    rid = data.get("ob_rid")
    n_total = len(failed_scoring)
    by_reason: Dict[str, List[str]] = {}
    for fs in failed_scoring:
        by_reason.setdefault(fs["reason"], []).append(fs["cluster"])

    lines: List[str] = [f"⚠ <b>Ozon scoring отбил все {n_total} кластеров.</b>", ""]
    if "NO_TIMESLOTS" in by_reason:
        cls = by_reason["NO_TIMESLOTS"]
        lines.append(
            f"🚫 <b>Нет таймслотов</b> у drop-off-хаба на эти даты "
            f"({len(cls)}): {', '.join(cls)}"
        )
    if "INVALID_ROUTE" in by_reason:
        cls = by_reason["INVALID_ROUTE"]
        lines.append(
            f"🚫 <b>Маршрут не обслуживается</b> из drop-off-хаба "
            f"({len(cls)}): {', '.join(cls)}"
        )
    if "OUT_OF_ASSORTMENT" in by_reason:
        cls = by_reason["OUT_OF_ASSORTMENT"]
        lines.append(
            f"🚫 <b>Товар не в ассортименте</b> кластера "
            f"({len(cls)}): {', '.join(cls)}"
        )
    if "OTHER" in by_reason:
        cls = by_reason["OTHER"]
        lines.append(
            f"🚫 <b>Прочая причина</b> ({len(cls)}): {', '.join(cls)} "
            f"— см. сообщение выше."
        )

    lines.append("")
    lines.append("<b>Что делать:</b>")
    transient = "NO_TIMESLOTS" in by_reason or "INVALID_ROUTE" in by_reason
    if transient:
        lines.append("• Расширь диапазон дат (минимум неделя)")
        lines.append("• Выбери другой drop-off хаб через ⭐ Точки кроссдока в карточке")
    if "OUT_OF_ASSORTMENT" in by_reason:
        lines.append(
            "• Проверь карточку товара в Seller Center → «Доступность по кластерам»"
        )

    # Для транзиентных причин (NO_TIMESLOTS / INVALID_ROUTE) стартуем auto-poll
    # на 60 мин — Ozon-логистика иногда добавляет слоты в течение часа, особенно
    # если даты дальние. Юзер: «почему тут не ставится на час поиск слотов».
    # Для OUT_OF_ASSORTMENT поллинг бесполезен — нужна правка карточки товара.
    rows: List[List[InlineKeyboardButton]] = []
    auto_poll_started = False
    if transient and rid is not None:
        existing = _AUTO_POLL_TASKS.get(rid)
        already_running = bool(existing and not existing.done())
        if not already_running:
            draft_type = data.get("ob_type") or "CREATE_TYPE_DIRECT"
            # Маркируем drafts «созданными 30 мин назад» — на первой итерации auto-poll
            # триггерит `_recreate_draft_for_auto_poll` (recreate_after=28мин), который
            # пересоздаёт draft свежим. draft_id=0 будет заменён на новый.
            poll_drafts: List[Dict] = []
            stale_ts = _time.time() - 30 * 60
            for fs in failed_scoring:
                if fs.get("reason") not in ("NO_TIMESLOTS", "INVALID_ROUTE"):
                    continue
                if not fs.get("cluster_id"):
                    continue  # без cluster_id пересоздать draft нельзя
                poll_drafts.append({
                    "cluster": fs["cluster"],
                    "cluster_id": fs["cluster_id"],
                    "supply_type": fs.get("supply_type", 2),
                    "draft_id": 0,  # будет пересоздан в первой итерации
                    "drop_off_warehouse_id": fs.get("drop_off_warehouse_id"),
                    "drop_off_warehouse_name": fs.get("drop_off_warehouse_name"),
                    "draft_type": draft_type,
                    "created_ts": stale_ts,
                })
            if poll_drafts:
                task = asyncio.create_task(_auto_poll_slots(
                    msg.bot, msg.chat.id, rid, poll_drafts,
                    data.get("ob_date_from_iso") or f"{data.get('ob_date_from')}T00:00:00Z",
                    data.get("ob_date_to_iso") or f"{data.get('ob_date_to')}T23:59:59Z",
                    data.get("ob_date_picks") or [],
                    data.get("ob_hour_picks") or [],
                    tg_id=data.get("ob_tg_id"),
                ))
                _AUTO_POLL_TASKS[rid] = task
                auto_poll_started = True
                rows.append([InlineKeyboardButton(
                    text="✖ Остановить авто-поиск",
                    callback_data=f"obcancelpoll:{rid}",
                )])

    if auto_poll_started:
        lines.append("")
        lines.append(
            "♻ <b>Авто-поиск в фоне запущен на 60 мин</b> — пересоздаю drafts каждые "
            "~28 мин. Если Ozon добавит слоты, придёт сообщение со списком."
        )

    rows.append([InlineKeyboardButton(
        text="◀ К карточке заявки",
        callback_data=f"ship_open:{rid}",
    )])
    await msg.answer(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


async def _show_scored_warehouse_picker(msg: Message, state: FSMContext) -> None:
    """Показать кнопки scored складов для текущего кластера.

    Для CROSSDOCK Ozon в scoring не возвращает конкретные РФЦ назначения
    (Ozon сам развезёт). В этом случае wh_list пустой — пропускаем picker
    и идём сразу к timeslot/info для всех drafts."""
    data = await state.get_data()
    clusters = data["ob_clusters"]
    idx = data.get("ob_cluster_idx", 0)
    draft_type = (data.get("ob_type") or "").upper()
    is_crossdock = "CROSSDOCK" in draft_type

    # CROSSDOCK: складов выбирать не нужно, сразу к таймслотам
    if is_crossdock:
        await progress_add(
            msg, state,
            "✅ Scoring готов. Для CROSSDOCK РФЦ определяет Ozon — иду к таймслотам.",
        )
        await state.update_data(
            ob_drafts=data.get("ob_drafts"),
            ob_date_from_iso=data.get("ob_date_from_iso"),
            ob_date_to_iso=data.get("ob_date_to_iso"),
        )
        await _fetch_slots_for_drafts(msg, state)
        return

    if idx >= len(clusters):
        await msg.answer("✅ Все кластеры выбраны.")
        await _release_wizard_for_state(state)
        await state.clear()
        return

    cluster = clusters[idx]
    scored = (data.get("ob_scored") or {}).get(cluster) or []
    available = [w for w in scored if w["available"]]
    unavailable = [w for w in scored if not w["available"]]

    # AUTO MODE: после успешного booking'а предыдущего направления юзер нажал
    # «🚀 Бронировать следующее» → флаг `ob_auto_walk` пришёл из state. Picker
    # не показываем, сразу запускаем _run_autowalk. Без этого юзер видел
    # лишний шаг «выбери склад» каждый раз для каждого направления.
    if data.get("ob_auto_walk") and available:
        await progress_add(
            msg, state,
            f"🚀 Авто-режим: запускаю Auto-walk для «{cluster}» ({len(available)} складов)."
        )
        await _run_autowalk(msg, state, idx)
        return

    if not available:
        # Все scored склады недоступны → показать причины
        lines = [f"🔴 <b>«{cluster}»</b>: Ozon scoring не выдал ни одного доступного склада."]
        if unavailable:
            lines.append("\nПричины:")
            for w in unavailable[:10]:
                lines.append(f"  • {w['name']}: {w['reason']}")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀ К карточке заявки", callback_data=f"ship_open:{data['ob_rid']}")],
        ])
        await msg.answer("\n".join(lines), reply_markup=kb)
        return

    rows: List[List[InlineKeyboardButton]] = []
    # Авто-режим — бот сам пройдёт по списку до первого 200 со слотами
    rows.append([InlineKeyboardButton(
        text="🚀 Auto-walk (бот сам найдёт)",
        callback_data=f"obautowalk:{idx}",
    )])
    for w in available[:15]:
        rank_emoji = "🥇" if w["rank"] == 1 else ("🥈" if w["rank"] == 2 else ("🥉" if w["rank"] == 3 else "🎯"))
        label = f"{rank_emoji} {w['name'][:30]}"
        rows.append([InlineKeyboardButton(
            text=label,
            callback_data=f"obscored:{idx}:{w['wh_id']}",
        )])
    rows.append([InlineKeyboardButton(text="◀ К карточке заявки",
                                      callback_data=f"ship_open:{data['ob_rid']}")])

    # Сведения о недоступных складах — в теле сообщения, без отдельной мёртвой
    # кнопки (раньше «ℹ Недоступно: N (скрыто)» вёл в `obscored_noop` → alert,
    # юзеру это место мешало больше чем помогало).
    unavail_note = (
        f"\n\n<i>ℹ Ещё {len(unavailable)} склад{'ов' if len(unavailable) != 1 else ''} "
        f"скрыт{'ы' if len(unavailable) != 1 else ''} (scoring недоступен).</i>"
        if unavailable else ""
    )
    progress = f"({idx + 1}/{len(clusters)})" if len(clusters) > 1 else ""
    await state.set_state(OzonBook.pick_warehouse)
    await msg.answer(
        f"📍 <b>«{cluster}» {progress}</b> — {len(available)} складов\n\n"
        f"«🚀 Auto-walk» — бот сам пойдёт по списку, остановится на первом со слотами.\n"
        f"«🥇🥈🥉🎯» — выбрать конкретный (мб 404 «not in scoring» или 0 слотов)."
        f"{unavail_note}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("obautowalk:"))
async def cb_ob_autowalk(cb: CallbackQuery, state: FSMContext) -> None:
    """Юзер тапнул «🚀 Auto-walk» — обёртка над `_run_autowalk`."""
    idx = int(cb.data.split(":", 1)[1])
    await cb.answer("Запускаю auto-walk…")
    if not cb.message:
        return
    await _run_autowalk(cb.message, state, idx)


async def _run_autowalk(msg: Message, state: FSMContext, idx: int) -> None:
    """Бот сам пробует scored-склады кластера idx пока не получит 200 со слотами.

    Вынесено из `cb_ob_autowalk` — теперь зовётся и при ручном клике юзера, и
    автоматом из `_show_scored_warehouse_picker` если `ob_auto_walk=True`
    (например после успешного бронирования предыдущего направления).
    """
    data = await state.get_data()
    cluster = data["ob_clusters"][idx]
    scored = (data.get("ob_scored") or {}).get(cluster) or []
    available = [w for w in scored if w["available"]]

    if not available:
        await msg.answer("⚠ Нет складов для перебора.")
        return

    # Готовим draft для текущего кластера
    drafts = data.get("ob_drafts") or []
    draft = next((d for d in drafts if d["cluster"] == cluster), None)
    if not draft:
        await msg.answer("⚠ Draft не найден.")
        return

    date_from_iso = data["ob_date_from_iso"]
    date_to_iso = data["ob_date_to_iso"]
    # Hour-фильтр (юзер выбрал на time-picker'е). Если пусто — фильтра нет.
    hour_picks_set = set(int(h) for h in (data.get("ob_hour_picks") or []))
    oz = await _ozon_client_from_state(state)
    if oz is None:
        await msg.answer(_NO_OZON_KEYS_MSG)
        return

    found_slots: List[Dict] = []
    tried = 0
    summary: List[str] = []  # отчёт по каждому wh для финального лога
    status_msg = await msg.answer(
        f"🔄 Auto-walk «{cluster}»: пробую {len(available)} складов.\n"
        f"⏳ Жду 10 сек чтобы Ozon успел рассчитать scoring…"
    )
    await asyncio.sleep(10.0)  # дать scoring-engine время «переварить» draft

    async def _try_wh(w: Dict, attempt: int) -> Tuple[str, List[Dict]]:
        """Возвращает (статус, найденные_слоты). Статус: 'slots'|'empty'|'404'|'429'|'err'."""
        try:
            ts = await oz.draft_timeslot_info(
                draft_id=draft["draft_id"],
                date_from=date_from_iso,
                date_to=date_to_iso,
                warehouse_ids=[w["wh_id"]],
                cluster_id=draft["cluster_id"],
                supply_type=draft.get("supply_type", 2),
                retries_on_429=2,
            )
        except OzonAPIError as e:
            s = str(e)
            if "404" in s and "scoring" in s.lower():
                return ("404", [])
            if "429" in s:
                return ("429", [])
            return ("err", [])

        entries = _parse_v2_timeslots(ts, fallback_wh_id=w["wh_id"], fallback_wh_name=w["name"])
        # Применяем hour-фильтр (если задан в state) — auto-walk должен уважать
        # выбранные часы как и обычный flow.
        if hour_picks_set:
            entries = [
                e for e in entries
                if (len(e.get("from", "")) >= 13) and int(e["from"][11:13]) in hour_picks_set
            ]
        slots: List[Dict] = []
        for e in entries:
            slots.append({
                "draft_id": draft["draft_id"],
                "cluster": cluster,
                "cluster_id": draft["cluster_id"],
                "supply_type": draft.get("supply_type", 2),
                "warehouse_id": e["warehouse_id"] or w["wh_id"],
                "warehouse_name": e["warehouse_name"] or w["name"],
                "from": e["from"],
                "to": e["to"],
            })
        return ("slots" if slots else "empty", slots)

    for w in available[:10]:
        tried += 1
        try:
            await status_msg.edit_text(
                f"🔄 Auto-walk {tried}/{min(10, len(available))}: <b>{w['name'][:30]}</b>…"
            )
        except Exception:
            pass

        # Первая попытка
        status, slots = await _try_wh(w, attempt=1)

        # При 404 «scoring not ready» — ждём 15 сек и пробуем ТОТ ЖЕ wh ещё раз
        if status == "404":
            try:
                await status_msg.edit_text(
                    f"🔄 {tried}/{min(10, len(available))}: <b>{w['name'][:30]}</b> "
                    f"→ 404 scoring not ready, жду 15 сек и пробую снова…"
                )
            except Exception:
                pass
            await asyncio.sleep(15.0)
            status, slots = await _try_wh(w, attempt=2)

        if status == "slots":
            summary.append(f"  ✅ {w['name'][:30]} — {len(slots)} слотов")
            found_slots.extend(slots)
            break
        elif status == "empty":
            summary.append(f"  ⚪ {w['name'][:30]} — 0 слотов (нет на даты)")
        elif status == "404":
            summary.append(f"  🔴 {w['name'][:30]} — not in scoring (даже после retry)")
        elif status == "429":
            # 429 при auto-walk = Ozon нас банит. Останавливаем чтобы не усугублять.
            summary.append(f"  ⏸ {w['name'][:30]} — 429 (Ozon банит, СТОП auto-walk)")
            try:
                await status_msg.edit_text(
                    f"⏸ Auto-walk остановлен на {tried}-м складе: Ozon банит наш аккаунт за частые запросы.\n"
                    f"Подожди 15-30 мин без активности и попробуй один раз вручную.\n\n"
                    + "\n".join(summary)
                )
            except Exception:
                pass
            return
        else:
            summary.append(f"  ❌ {w['name'][:30]} — ошибка")

        await asyncio.sleep(3.0)  # пауза между складами — щадим Ozon

    if found_slots:
        try:
            await status_msg.edit_text(
                f"🎉 Auto-walk нашёл слоты на <b>{found_slots[0]['warehouse_name']}</b>!\n\n"
                + "\n".join(summary)
            )
        except Exception:
            pass
    else:
        try:
            await status_msg.edit_text(
                f"🔴 Auto-walk прошёлся по {tried} складам:\n\n"
                + "\n".join(summary) + "\n\n"
                "Если много «not in scoring» — Ozon scoring-engine перегружен, "
                "попробуй через 5-10 мин. Если много «0 слотов» — реально нет на эти даты, "
                "расширь даты в карточке."
            )
        except Exception:
            pass
        return

    # Постим найденные слоты
    await _post_found_slots(
        cb.message.bot, cb.message.chat.id, data["ob_rid"], found_slots,
        tg_id=int(data.get("ob_tg_id") or 0),
    )


@router.callback_query(F.data.startswith("obscored:"))
async def cb_ob_scored_pick(cb: CallbackQuery, state: FSMContext) -> None:
    parts = cb.data.split(":")
    idx = int(parts[1])
    wh_id = int(parts[2])
    data = await state.get_data()
    cluster = data["ob_clusters"][idx]
    scored = (data.get("ob_scored") or {}).get(cluster) or []
    wh_name = next((w["name"] for w in scored if w["wh_id"] == wh_id), f"#{wh_id}")

    # Записываем выбор и сразу запускаем timeslot/info
    choices = dict(data.get("ob_wh_choices") or {})
    choices[cluster] = wh_id
    # Также обновим drafts_made с wh_id для timeslot
    drafts = data.get("ob_drafts") or []
    for d in drafts:
        if d["cluster"] == cluster:
            d["wh_id"] = wh_id
    await state.update_data(ob_wh_choices=choices, ob_drafts=drafts)
    await cb.answer(f"Выбран: {wh_name[:30]}")
    if cb.message:
        await safe_edit_or_answer(cb.message, f"✅ «{cluster}» → <b>{wh_name}</b>\n\n⏳ Тяну слоты…")
        # Только один кластер сейчас — сразу к timeslot. Если кластеров больше — после всех.
        if idx + 1 < len(data["ob_clusters"]):
            await state.update_data(ob_cluster_idx=idx + 1)
            await _show_scored_warehouse_picker(cb.message, state)
        else:
            await _fetch_slots_for_drafts(cb.message, state)


async def _ask_warehouse_for_cluster(msg: Message, state: FSMContext) -> None:
    """Показать выбор склада для текущего кластера (ob_cluster_idx)."""
    data = await state.get_data()
    clusters = data["ob_clusters"]
    idx = data.get("ob_cluster_idx", 0)
    if idx >= len(clusters):
        # Все кластеры выбраны — запускаем создание drafts
        await _create_drafts(msg, state)
        return

    cluster = clusters[idx]
    warehouses = _get_cluster_ff_warehouses(cluster)
    if not warehouses:
        await msg.answer(
            f"⚠ Для кластера «{cluster}» нет данных по складам в кэше. "
            f"Запусти «🛠 Диагностика → 🏭 Ozon кластеры FBO» для прогрева кэша."
        )
        # Сохраним выбор «любой» и движемся дальше
        choices = dict(data.get("ob_wh_choices") or {})
        choices[cluster] = None
        await state.update_data(ob_wh_choices=choices, ob_cluster_idx=idx + 1)
        await _ask_warehouse_for_cluster(msg, state)
        return

    # Топ-6 складов + «Показать все». «Без фильтра» не работает на v2 — требует
    # конкретный storage_warehouse_id. Если выбранный wh не в Ozon-scoring draft'а,
    # будет 404 — придётся пробовать другой.
    rows: List[List[InlineKeyboardButton]] = []
    for w in warehouses[:6]:
        rows.append([InlineKeyboardButton(
            text=f"🎯 {w['name'][:32]}",
            callback_data=f"obwh:{idx}:{w['wh_id']}",
        )])
    if len(warehouses) > 6:
        rows.append([InlineKeyboardButton(
            text=f"📋 Показать все ({len(warehouses)})",
            callback_data=f"obwhall:{idx}",
        )])
    rows.append([InlineKeyboardButton(text="✖ Отмена", callback_data="cancel")])

    progress = f"({idx + 1}/{len(clusters)})" if len(clusters) > 1 else ""
    await state.set_state(OzonBook.pick_warehouse)
    await msg.answer(
        f"📍 <b>Куда грузим в кластере «{cluster}» {progress}?</b>\n\n"
        f"Выбери конкретный склад. Если Ozon вернёт 404 «not in scoring» — "
        f"значит этот склад не подходит для текущего draft, попробуй другой "
        f"(scoring алгоритм Ozon смотрит на товары + кластер и допускает только "
        f"часть РФЦ).",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(OzonBook.pick_warehouse, F.data.startswith("obwhall:"))
async def cb_ob_wh_all(cb: CallbackQuery, state: FSMContext) -> None:
    """Показать ВСЕ склады кластера (без фильтра топ-6)."""
    idx = int(cb.data.split(":", 1)[1])
    data = await state.get_data()
    cluster = data["ob_clusters"][idx]
    warehouses = _get_cluster_ff_warehouses(cluster)
    rows: List[List[InlineKeyboardButton]] = []
    for w in warehouses:
        rows.append([InlineKeyboardButton(
            text=f"🎯 {w['name'][:35]}",
            callback_data=f"obwh:{idx}:{w['wh_id']}",
        )])
    rows.append([InlineKeyboardButton(text="◀ Назад к топу", callback_data=f"obwhback:{idx}")])
    rows.append([InlineKeyboardButton(text="✖ Отмена", callback_data="cancel")])
    await cb.answer()
    if cb.message:
        await safe_edit_or_answer(
            cb.message,
            f"📍 Все склады кластера «{cluster}» ({len(warehouses)}):",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )


@router.callback_query(OzonBook.pick_warehouse, F.data.startswith("obwhback:"))
async def cb_ob_wh_back(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    if cb.message:
        await _ask_warehouse_for_cluster(cb.message, state)


@router.callback_query(OzonBook.pick_warehouse, F.data.startswith("obwh:"))
async def cb_ob_wh_pick(cb: CallbackQuery, state: FSMContext) -> None:
    """Пользователь выбрал склад (или «Любой» / «Без фильтра»)."""
    parts = cb.data.split(":")
    data = await state.get_data()
    # формат: obwh:any:<idx> | obwh:none:<idx> | obwh:<idx>:<wh_id>
    if parts[1] == "none":
        idx = int(parts[2])
        cluster = data["ob_clusters"][idx]
        wh_id = None
        wh_name = "(все склады кластера, без фильтра)"
    elif parts[1] == "any":
        idx = int(parts[2])
        cluster = data["ob_clusters"][idx]
        warehouses = _get_cluster_ff_warehouses(cluster)
        wh_id = warehouses[0]["wh_id"] if warehouses else None
        wh_name = warehouses[0]["name"] if warehouses else "(не определён)"
    else:
        idx = int(parts[1])
        wh_id = int(parts[2])
        cluster = data["ob_clusters"][idx]
        warehouses = _get_cluster_ff_warehouses(cluster)
        wh_name = next((w["name"] for w in warehouses if w["wh_id"] == wh_id), f"#{wh_id}")

    choices = dict(data.get("ob_wh_choices") or {})
    choices[cluster] = wh_id
    await state.update_data(ob_wh_choices=choices, ob_cluster_idx=idx + 1)
    await cb.answer(f"Записал: {wh_name[:30]}")
    if cb.message:
        await safe_edit_or_answer(
            cb.message,
            f"✅ «{cluster}» → <b>{wh_name}</b>",
        )
        # Переходим к следующему кластеру или к созданию drafts
        await _ask_warehouse_for_cluster(cb.message, state)


@router.callback_query(OzonBook.pick_type, F.data.startswith("obtype:"))
async def cb_ob_type(cb: CallbackQuery, state: FSMContext) -> None:
    mode = cb.data.split(":", 1)[1]
    draft_type = "CREATE_TYPE_CROSSDOCK" if mode == "cross" else "CREATE_TYPE_DIRECT"
    await state.update_data(ob_type=draft_type)
    await cb.answer("Создаю draft в Ozon…")
    if cb.message:
        info_txt = (
            f"⏳ Создаю Ozon draft (type={draft_type})…\n"
            "Запрос API, polling статуса (~5-30 сек).\n"
        )
        if draft_type == "CREATE_TYPE_CROSSDOCK":
            info_txt += (
                "\n💡 <b>Drop-off склад</b> выберешь после: Ozon вернёт список доступных "
                "приёмочных пунктов МСК со слотами — тапнешь нужный."
            )
        await safe_edit_or_answer(cb.message, info_txt)
        await _create_drafts(cb.message, state)


async def _create_drafts(msg: Message, state: FSMContext) -> None:
    data = await state.get_data()
    rid = data["ob_rid"]
    clusters = data["ob_clusters"]
    draft_type = data["ob_type"]
    date_from = data["ob_date_from"]
    date_to = data["ob_date_to"]
    wh_choices = data.get("ob_wh_choices") or {}

    oz = await _ozon_client_from_state(state)
    if oz is None:
        await msg.answer(_NO_OZON_KEYS_MSG)
        return

    # Pre-check SKU (см. _create_drafts_and_fetch_scoring — та же логика)
    all_skus_to_check: List[int] = []
    tg_id_cd = int(data.get("ob_tg_id") or 0)
    with db_session() as session:
        req = get_shipment_request(session, rid, user_id=tg_id_cd)
        if req:
            for cl in clusters:
                items_check, _ = _build_items_for_cluster(req, cl)
                for it in items_check:
                    if it["sku"] not in all_skus_to_check:
                        all_skus_to_check.append(it["sku"])
    # Продолжаем «сардельку» от _start_ozon_book_wizard или создаём новую
    await progress_start(msg, state, "\n⚙ <b>Создаю поставку Ozon…</b>")

    if all_skus_to_check:
        await progress_add(msg, state, "🔍 Сверяю SKU с актуальным Ozon-кабинетом…")
        missing, _ = await _validate_skus_in_current_account(oz, all_skus_to_check)
        if missing:
            lines: List[str] = []
            with db_session() as session:
                from src.db.models import OzonProduct
                bad = session.query(OzonProduct).filter(OzonProduct.sku.in_(missing)).all()
                seen_sku = set()
                for p in bad:
                    seen_sku.add(p.sku)
                    lines.append(
                        f"  • <code>{p.offer_id}</code> (sku=<code>{p.sku}</code>)"
                    )
                for s in missing:
                    if s not in seen_sku:
                        lines.append(f"  • sku=<code>{s}</code> (нет в нашей БД)")
            await progress_add(
                msg, state,
                f"🚫 <b>Стоп — артикулы не из текущего кабинета.</b>\n"
                + "\n".join(lines[:15])
                + ("\n  …" if len(lines) > 15 else "")
                + "\n<i>Открой меню → 🔗 Привязать каталог → /sku_link_ozon</i>"
            )
            await _release_wizard_for_state(state)
            await state.clear()
            return

    drafts_made: List[Dict] = []
    for cl in clusters:
        wh_id = wh_choices.get(cl)
        wh_label = ""
        if wh_id:
            wh_list = _get_cluster_ff_warehouses(cl)
            wh_name = next((w["name"] for w in wh_list if w["wh_id"] == wh_id), f"#{wh_id}")
            wh_label = f" → {wh_name}"
        await progress_add(msg, state, f"🔄 Кластер <b>«{cl}»</b>{wh_label}…")

        try:
            cid = await _resolve_ozon_cluster_id(oz, cl)
        except OzonAPIError as e:
            await progress_add(msg, state, f"  ⚠ cluster_list: <code>{str(e)[:200]}</code>")
            continue
        if not cid:
            await progress_add(msg, state, f"  ⚠ Не сматчил «{cl}». Пропускаю.")
            continue

        with db_session() as session:
            req = get_shipment_request(session, rid, user_id=tg_id_cd)
            if not req:
                await progress_add(msg, state, f"Заявка #{rid} пропала.")
                return
            items, missing = _build_items_for_cluster(req, cl)

        if not items:
            await progress_add(msg, state, f"  ⚠ «{cl}»: нет SKU с offer_id — пропуск.")
            continue

        wh_log = f" (склад #{wh_id})" if wh_id else ""
        await progress_add(
            msg, state,
            f"  📦 Создаю расчёт{wh_log}: {len(items)} SKU. Подождём 15с…"
        )
        await asyncio.sleep(15.0)
        try:
            op_id = await oz.draft_create(
                items=items,
                cluster_ids=[cid],
                draft_type=draft_type,
            )
        except OzonAPIError as e:
            await progress_add(msg, state, f"  ❌ Не получилось: <code>{str(e)[:400]}</code>")
            continue

        if op_id.startswith("sync:"):
            draft_id = op_id.split(":", 1)[1]
        else:
            await progress_add(msg, state, "  ⏳ Ждём Ozon…")
            info = await _wait_draft_ready(oz, op_id)
            status = info.get("status", "?")
            if "SUCCESS" not in status.upper() and "DONE" not in status.upper():
                errs = info.get("errors") or []
                err_s = "; ".join(str(e)[:120] for e in errs[:3]) if errs else "?"
                await progress_add(
                    msg, state,
                    f"  ❌ Расчёт не готов: {status} / {err_s}"
                )
                continue
            draft_id = info.get("draft_id") or info.get("calculation_id")
        if not draft_id:
            await progress_add(msg, state, "  ⚠ Ozon не вернул расчёт.")
            continue

        supply_type_int = 1 if "CROSSDOCK" in (draft_type or "").upper() else 2
        drafts_made.append({
            "cluster": cl,
            "cluster_id": cid,
            "draft_id": int(draft_id),
            "operation_id": op_id,
            "items_count": len(items),
            "wh_id": wh_id,
            "supply_type": supply_type_int,
        })
        await progress_add(msg, state, f"  ✅ Расчёт #{draft_id} готов")

    if not drafts_made:
        await progress_add(msg, state, "⚠ Ни один draft не создан.")
        await _release_wizard_for_state(state)
        await state.clear()
        return

    await state.update_data(
        ob_drafts=drafts_made,
        ob_date_from_iso=f"{date_from}T00:00:00Z",
        ob_date_to_iso=f"{date_to}T23:59:59Z",
    )

    await _fetch_slots_for_drafts(msg, state)


async def _fetch_slots_for_drafts(msg: Message, state: FSMContext) -> None:
    """Тянем таймслоты для всех созданных drafts. Можно перезапускать (retry-кнопкой).

    Ожидаем в state: ob_drafts (list dicts с draft_id/cluster), ob_date_from_iso,
    ob_date_to_iso, ob_rid.
    """
    data = await state.get_data()
    drafts_made = data.get("ob_drafts") or []
    date_from_iso = data.get("ob_date_from_iso")
    date_to_iso = data.get("ob_date_to_iso")
    rid = data.get("ob_rid")
    if not drafts_made or not date_from_iso:
        await msg.answer("⚠ Нет данных о drafts — пересоздать через карточку заявки.")
        await state.clear()
        return

    oz = await _ozon_client_from_state(state)
    if oz is None:
        await msg.answer(_NO_OZON_KEYS_MSG)
        return
    clusters_with_slots: List[Dict] = []  # для нового picker'а (per-cluster)
    failed_drafts: List[Dict] = []  # для возможного retry

    # Даты уже забронированных кластеров — чтобы подсвечивать «✓ та же дата»
    # в слотах следующих кластеров (синхронизировать отгрузку по датам).
    booked_dates: set = set()
    tg_id_fs = int(data.get("ob_tg_id") or 0)
    if rid:
        with db_session() as session:
            req = get_shipment_request(session, rid, user_id=tg_id_fs)
            if req:
                for it in req.items:
                    if it.marketplace == "ozon" and it.booked_slot_at:
                        booked_dates.add(it.booked_slot_at.date().isoformat())

    for d in drafts_made:
        wh_id_filter = d.get("wh_id")
        wh_suffix = f" / wh={wh_id_filter}" if wh_id_filter else ""
        await progress_add(
            msg, state,
            f"📅 Таймслоты draft #{d['draft_id']} ({d['cluster']}){wh_suffix}…"
        )
        await asyncio.sleep(3.0)
        try:
            ts = await oz.draft_timeslot_info(
                draft_id=d["draft_id"],
                date_from=date_from_iso,
                date_to=date_to_iso,
                warehouse_ids=[wh_id_filter] if wh_id_filter else None,
                cluster_id=d.get("cluster_id"),
                supply_type=d.get("supply_type", 2),
            )
        except OzonAPIError as e:
            err_str = str(e)
            # Специальный кейс: "can't find any calculation tasks" — у draft'a
            # вообще нет scoring задач. Обычно это значит что у выбранной
            # drop-off-точки нет таймслотов для этого кластера (Ozon-логистика
            # не возит из этой точки в этот кластер на эти даты).
            if "calculation tasks" in err_str.lower() or "scoring result" in err_str.lower():
                await progress_add(
                    msg, state,
                    f"  🚫 «{d['cluster']}»: у drop-off-точки нет таймслотов "
                    f"для этого кластера. Выбери другой хаб."
                )
                # Инвалидируем cached draft — при повторе запросит drop-off заново
                await _invalidate_failed_draft(d["draft_id"])
            else:
                await progress_add(
                    msg, state,
                    f"  ⚠ timeslot/info «{d['cluster']}»: <code>{err_str[:200]}</code>"
                )
            failed_drafts.append(d)
            continue

        # Парсим ответ v2: данные под "result.drop_off_warehouse_timeslots"
        parsed_slots = _parse_v2_timeslots(ts, fallback_wh_id=wh_id_filter, fallback_wh_name="")
        if not parsed_slots:
            await progress_add(msg, state, f"  🔴 «{d['cluster']}» — пустой ответ timeslot/info")
            continue

        # Фильтруем по выбранным пользователем датам
        date_picks = data.get("ob_date_picks") or []
        if date_picks:
            picks_set = set(date_picks)
            total_before = len(parsed_slots)
            parsed_slots = [e for e in parsed_slots if e["from"][:10] in picks_set]
            if total_before and not parsed_slots:
                await progress_add(
                    msg, state,
                    f"  🔴 «{d['cluster']}»: слотов на выбранные даты "
                    f"({', '.join(sorted(picks_set))}) нет."
                )
                continue

        # Фильтруем по выбранным часам (если указаны). Час старта слота —
        # `slot.from[11:13]` (формат "YYYY-MM-DDTHH:MM:SS...").
        hour_picks = data.get("ob_hour_picks") or []
        if hour_picks:
            hours_set = {int(h) for h in hour_picks}
            total_before = len(parsed_slots)
            parsed_slots = [
                e for e in parsed_slots
                if (len(e.get("from", "")) >= 13) and int(e["from"][11:13]) in hours_set
            ]
            if total_before and not parsed_slots:
                from src.bot.helpers import format_picked_hours
                await progress_add(
                    msg, state,
                    f"  🔴 «{d['cluster']}»: слотов в выбранные часы "
                    f"({format_picked_hours(list(hours_set))}) нет."
                )
                continue

        # Сортируем по дате-времени, сохраняем ВСЕ слоты этого кластера —
        # picker покажет их со страничной навигацией.
        parsed_slots.sort(key=lambda e: e["from"])
        dropoff_choices = data.get("ob_dropoff_choices") or {}
        dropoff_name = (dropoff_choices.get(d["cluster"]) or {}).get("name") or ""
        clusters_with_slots.append({
            "cluster": d["cluster"],
            "draft_id": d["draft_id"],
            "cluster_id": d.get("cluster_id"),
            "supply_type": d.get("supply_type", 2),
            "drop_off_name": dropoff_name,
            "slots": parsed_slots,
        })
        await progress_add(msg, state, f"🟢 <b>{d['cluster']}</b> — {len(parsed_slots)} слотов")

    if clusters_with_slots:
        # Имена failed-кластеров — показываем их в обзоре с пометкой «нужен другой drop-off».
        failed_cluster_names = [d["cluster"] for d in failed_drafts]

        # AUTO-BOOK MODE: если юзер на time-picker'е выбрал часы (а не «🎲 любое
        # время»), это значит «хочу любой слот в этом окне, сам не выбираю».
        # Стратегия выбора: БЛИЖАЙШАЯ дата + ПОСЛЕДНИЙ слот этого дня.
        # Логика «последний слот»: даём максимум времени на упаковку до отгрузки.
        # Логика «ближайшая дата»: чем раньше отгрузим — тем быстрее товар на склад.
        hour_picks = data.get("ob_hour_picks") or []
        if hour_picks:
            auto_choices: Dict[str, Dict] = {}
            for i, c in enumerate(clusters_with_slots):
                slots = c.get("slots") or []
                if not slots:
                    continue
                # slots уже sorted by 'from' выше. Берём дату первого = ближайшая
                # с доступными слотами (и попавшая в hour_picks).
                earliest_date = slots[0]["from"][:10]
                same_day = [s for s in slots if s["from"][:10] == earliest_date]
                chosen = same_day[-1]  # последний — самый поздний час в окне дня
                auto_choices[str(i)] = {
                    "cluster": c["cluster"],
                    "cluster_id": c.get("cluster_id"),
                    "draft_id": c["draft_id"],
                    "supply_type": c.get("supply_type", 2),
                    "drop_off_name": c.get("drop_off_name"),
                    "warehouse_id": chosen["warehouse_id"],
                    "warehouse_name": chosen["warehouse_name"],
                    "from": chosen["from"],
                    "to": chosen["to"],
                }
            if auto_choices:
                from src.bot.helpers import format_picked_hours
                await progress_add(
                    msg, state,
                    f"\n🎯 <b>Авто-бронирование</b> по выбранным часам "
                    f"({format_picked_hours(hour_picks)}) — беру ПОСЛЕДНИЙ слот в окне на ближайшую доступную дату."
                )
                await state.update_data(
                    ob_picker_clusters=clusters_with_slots,
                    ob_picker_choices=auto_choices,
                    ob_failed_clusters=failed_cluster_names,
                )
                await state.set_state(OzonBook.pick_slot)
                await _run_bulk_book(msg.bot, msg, state)
                return

        await state.update_data(
            ob_picker_clusters=clusters_with_slots,
            ob_picker_idx=0,
            ob_picker_page=0,
            ob_picker_choices={},
            ob_picker_msg_id=None,
            ob_overview_msg_id=None,
            ob_failed_clusters=failed_cluster_names,
        )
        await state.set_state(OzonBook.pick_slot)
        await _render_date_overview(msg, state)
        return

    # Разделяем причины: 429 / 404 not-in-scoring / реально нет слотов
    rows: List[List[InlineKeyboardButton]] = []
    # Проверяем какая ошибка у первого failed (если есть)
    err_kind = ""
    if failed_drafts:
        # Подсмотрим в state последний текст ошибки — упрощённо
        # (мы её уже отправили пользователю выше, тут просто решаем что показать)
        # На этом этапе detect: 429 vs 404 нельзя без extra данных; делаем общий путь.
        err_kind = "unknown"

    if failed_drafts:
        # Обогащаем failed_drafts данными для возможного пересоздания (live > 28 мин):
        # drop_off_warehouse_id из cached draft + draft_type из state.
        dropoff_choices = data.get("ob_dropoff_choices") or {}
        draft_type = data.get("ob_type") or "CREATE_TYPE_DIRECT"
        for fd in failed_drafts:
            choice = dropoff_choices.get(fd["cluster"]) or {}
            fd["drop_off_warehouse_id"] = choice.get("wh_id")
            fd["drop_off_warehouse_name"] = choice.get("name")
            fd["draft_type"] = draft_type
            fd["created_ts"] = _time.time()

        existing = _AUTO_POLL_TASKS.get(rid)
        auto_running = bool(existing and not existing.done())
        if not auto_running:
            task = asyncio.create_task(_auto_poll_slots(
                msg.bot,
                msg.chat.id,
                rid,
                failed_drafts,
                data["ob_date_from_iso"],
                data["ob_date_to_iso"],
                data.get("ob_date_picks") or [],
                data.get("ob_hour_picks") or [],
                tg_id=data.get("ob_tg_id"),
            ))
            _AUTO_POLL_TASKS[rid] = task

        rows.append([InlineKeyboardButton(
            text="🔁 Повторить (тот же склад)",
            callback_data=f"obretry:{rid or 0}",
        )])
        rows.append([InlineKeyboardButton(
            text="◀ Выбрать другой склад",
            callback_data=f"ozon_book_card:{rid or 0}",
        )])
        rows.append([InlineKeyboardButton(
            text="✖ Остановить авто-поиск",
            callback_data=f"obcancelpoll:{rid or 0}",
        )])
        ids = ", ".join(str(d["draft_id"]) for d in failed_drafts)
        txt = (
            "⚠ <b>Не удалось получить слоты</b>.\n\n"
            "Возможные причины:\n"
            "• <b>429</b> — общий rate-limit Ozon (2 req/sec на всех продавцов).\n"
            "• <b>404 «not in scoring»</b> — выбранный склад не подходит для этого draft "
            "(Ozon scoring пропускает только часть РФЦ для каждых товаров).\n"
            "• <b>Слотов нет</b> на эти даты — расширь даты в карточке.\n\n"
            f"📝 Drafts: <code>{ids}</code> (живут 30 мин).\n\n"
            "♻ Авто-поиск в фоне запущен — если был 429 и лимит отпустит, придёт сообщение."
        )
    else:
        # Сюда — если запросы прошли, но ни одного слота в датах не вернулось
        txt = (
            "🔴 <b>Реально нет слотов</b> на выбранные даты у Ozon.\n\n"
            "Расширь диапазон дат через «🛠 Изменить даты» в карточке."
        )
    rows.append([InlineKeyboardButton(
        text="🌐 Ozon ЛК → Поставки",
        url="https://seller.ozon.ru/app/supply-orders",
    )])
    rows.append([InlineKeyboardButton(text="◀ К карточке заявки",
                                      callback_data=f"ship_open:{rid}")])
    await msg.answer(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


# ── Auto-poll: фоновое периодическое получение слотов после 429 ──────────


async def _recreate_draft_for_auto_poll(
    oz: OzonClient, rid: int, fd: Dict, tg_id: int,
) -> Optional[int]:
    """Пересоздаёт draft для failed_draft записи. items берём из БД по rid+cluster,
    drop_off / тип / cluster_id — из самой записи fd (обогащено caller'ом).

    Возвращает new draft_id или None если не вышло. Не кидает наружу — auto-poll
    должен переживать неудачу пересоздания (просто продолжит долбить старый).
    """
    try:
        with db_session() as session:
            req = get_shipment_request(session, rid, user_id=tg_id)
            if not req:
                return None
            items, _missing = _build_items_for_cluster(req, fd["cluster"])
        if not items:
            return None
        op_id = await oz.draft_create(
            items=items,
            cluster_ids=[int(fd["cluster_id"])] if fd.get("cluster_id") else None,
            draft_type=fd.get("draft_type") or "CREATE_TYPE_DIRECT",
            drop_off_point_warehouse_id=fd.get("drop_off_warehouse_id"),
        )
        if op_id.startswith("sync:"):
            new_id = int(op_id.split(":", 1)[1])
        else:
            info = await _wait_draft_ready(oz, op_id)
            new_id = int(info.get("draft_id") or info.get("calculation_id") or 0)
        if not new_id:
            return None
        # Сохраняем в кэш, чтобы UI знал про новый draft.
        from src.services.draft_cache import save_draft as _save_draft
        with db_session() as session:
            _save_draft(
                session, rid, fd["cluster"], int(fd.get("cluster_id") or 0), new_id,
                fd.get("supply_type", 2),
                drop_off_warehouse_id=fd.get("drop_off_warehouse_id"),
                drop_off_warehouse_name=fd.get("drop_off_warehouse_name"),
            )
        return new_id
    except Exception as e:
        logger.warning("auto-poll recreate draft failed (cluster=%s): %s",
                       fd.get("cluster"), e)
        return None


async def _silent_book_slot(
    bot, chat_id: int, rid: int, slot: Dict, tg_id: int,
) -> bool:
    """Бронирует слот без UI/state (для авто-брон из _auto_poll_slots).
    После успеха шлёт сообщение «✅ Самара забронирована…» в чат.
    Возвращает True/False. Никаких progress_add — мы вне FSM.
    """
    oz = _ozon_client_for_tg(tg_id) if tg_id else None
    if oz is None:
        return False
    cluster_id = slot.get("cluster_id")
    if not cluster_id:
        return False
    # supply/create с retry на 429
    saw_429 = False
    errors = None
    for attempt in range(3):
        try:
            errors = await oz.draft_supply_create_v2(
                draft_id=slot["draft_id"],
                cluster_id=int(cluster_id),
                warehouse_id=slot["warehouse_id"],
                timeslot_from=slot["from"],
                timeslot_to=slot["to"],
                supply_type=slot.get("supply_type", 2),
            )
            break
        except OzonAPIError as e:
            err_str = str(e)
            if "429" in err_str or "rate limit" in err_str.lower():
                saw_429 = True
                if attempt < 2:
                    await asyncio.sleep(45)
                    continue
            logger.warning("auto-poll silent_book supply/create failed: %s", e)
            return False
    if errors:
        return False
    # polling status
    final = None
    for _ in range(30):
        await asyncio.sleep(2)
        try:
            info = await oz.draft_supply_create_status_v2(slot["draft_id"])
        except OzonAPIError:
            return False
        status = str(info.get("status") or "").upper()
        if status in {"SUCCESS", "FAILED"}:
            final = info
            break
    if not final or str(final.get("status") or "").upper() != "SUCCESS":
        return False
    order_id = final.get("order_id")
    # Сохраняем в БД items
    with db_session() as session:
        req = get_shipment_request(session, rid, user_id=tg_id)
        if req and order_id:
            is_crossdock = _is_crossdock_wh(slot.get("warehouse_name"), slot.get("warehouse_id"))
            wh_to_save = None if is_crossdock else slot.get("warehouse_name")
            for it in req.items:
                if it.marketplace == "ozon" and it.cluster == slot["cluster"]:
                    it.booked_supply_id = str(order_id)
                    it.target_warehouse = wh_to_save
                    it.booked_slot_at = datetime.fromisoformat(
                        slot["from"].replace("Z", "+00:00").split("+")[0]
                    )
            from src.services.shipment_service import refresh_request_state_after_booking
            refresh_request_state_after_booking(req)
    drop_off_display = slot.get("drop_off_name") or slot.get("warehouse_name") or "—"
    slot_label = f"{slot['from'][:10]} {slot['from'][11:16]}"
    try:
        await bot.send_message(
            chat_id,
            f"🔔 <b>Авто-брон #{rid}: {slot['cluster']} забронирована!</b>\n"
            f"📌 {slot_label} · {drop_off_display}\n"
            f"order_id <code>{order_id}</code>",
        )
    except Exception:
        pass
    return True


async def _auto_poll_slots(
    bot,
    chat_id: int,
    rid: int,
    drafts: List[Dict],
    date_from_iso: str,
    date_to_iso: str,
    date_picks: Optional[List[str]] = None,
    hour_picks: Optional[List[int]] = None,
    tg_id: Optional[int] = None,
    auto_book: bool = False,
) -> None:
    """Раз в 60 сек дёргает timeslot/info. До 60 мин общая длительность.
    Каждые ~28 мин пересоздаёт drafts (Ozon draft живёт 30 мин). Если за час
    слотов нет — пишет финальное «не нашёл, братан» сообщение.

    `auto_book=True`: при нахождении слота сразу бронит через _silent_book_slot
    (нужно для авто-брон режима — юзер кликнул «🎯 В одну дату», ждёт автомат).
    `auto_book=False`: присылает inline-кнопки для ручного выбора слота (legacy).
    """
    import time as _t
    hour_set = {int(h) for h in (hour_picks or [])}

    deadline = _t.time() + 60 * 60   # 60 минут
    interval = 60                     # секунд между попытками
    recreate_after = 28 * 60          # пересоздаём draft если ему > 28 мин
    attempts = 0
    status_msg_id: Optional[int] = None

    logger.info("auto-poll started: rid=%d, drafts=%d", rid, len(drafts))
    try:
        # Первая попытка через 60 сек (initial-окно уже отработало в _fetch_slots_for_drafts)
        await asyncio.sleep(interval)

        while _t.time() < deadline:
            attempts += 1
            oz = _ozon_client_for_tg(tg_id) if tg_id else None
            if oz is None:
                logger.warning("auto-poll: no creds for tg_id=%s, abort", tg_id)
                return
            # Пересоздаём drafts которым >28 мин — иначе Ozon вернёт 404 expired.
            recreated_this_iter = 0
            for fd in drafts:
                if fd.get("created_ts") and _t.time() - fd["created_ts"] > recreate_after:
                    new_id = await _recreate_draft_for_auto_poll(oz, rid, fd, tg_id or 0)
                    if new_id:
                        logger.info("auto-poll: recreated draft for cluster=%s old=%d new=%d",
                                    fd["cluster"], fd["draft_id"], new_id)
                        fd["draft_id"] = new_id
                        fd["created_ts"] = _t.time()
                        recreated_this_iter += 1
            # Если в эту итерацию пересоздали хоть один draft — даём Ozon scoring'у
            # 30 сек чтобы посчитаться. Без этого timeslot/info на свежий draft
            # ловит 404 «scoring not found» и (раньше) убивал auto-poll.
            if recreated_this_iter:
                logger.info("auto-poll: recreated %d drafts, waiting 30s for scoring",
                            recreated_this_iter)
                await asyncio.sleep(30)
            all_slot_entries: List[Dict] = []
            any_429 = False
            any_ok = False

            for d in drafts:
                wh_id_filter = d.get("wh_id")
                try:
                    ts = await oz.draft_timeslot_info(
                        draft_id=d["draft_id"],
                        date_from=date_from_iso,
                        date_to=date_to_iso,
                        warehouse_ids=[wh_id_filter] if wh_id_filter else None,
                        cluster_id=d.get("cluster_id"),
                        supply_type=d.get("supply_type", 2),
                        retries_on_429=2,
                    )
                    any_ok = True
                    entries = _parse_v2_timeslots(
                        ts,
                        fallback_wh_id=wh_id_filter,
                        fallback_wh_name="",
                    )
                    if date_picks:
                        picks_set = set(date_picks)
                        entries = [e for e in entries if e["from"][:10] in picks_set]
                    if hour_set:
                        entries = [
                            e for e in entries
                            if (len(e.get("from", "")) >= 13)
                            and int(e["from"][11:13]) in hour_set
                        ]
                    for e in entries:
                        all_slot_entries.append({
                            "draft_id": d["draft_id"],
                            "cluster": d["cluster"],
                            "cluster_id": d.get("cluster_id"),
                            "supply_type": d.get("supply_type", 2),
                            "warehouse_id": e["warehouse_id"],
                            "warehouse_name": e["warehouse_name"],
                            "from": e["from"],
                            "to": e["to"],
                        })
                except OzonAPIError as e:
                    err_s = str(e)
                    if "429" in err_s:
                        any_429 = True
                        continue
                    # 404 «scoring result not found». Раньше всегда убивало
                    # auto-poll — но для scoring-fail-restart кейса (юзер тыкнул
                    # рестарт после NO_TIMESLOTS) draft только что пересоздан и
                    # scoring ещё не успел посчитаться. Различаем по возрасту:
                    # < 120 сек — даём шанс, > — связка реально невалидна.
                    if "404" in err_s and "scoring" in err_s.lower():
                        age = _t.time() - (d.get("created_ts") or 0)
                        if age < 120:
                            logger.info(
                                "auto-poll: 404 scoring on fresh draft %s (age %.0fs) "
                                "— ретрай в следующей итерации", d.get("draft_id"), age,
                            )
                            continue
                        await bot.send_message(
                            chat_id,
                            f"🛑 Авто-поиск #{rid} остановлен: 404 «scoring not found» "
                            f"даже на старом draft'е ({age:.0f}с). Связка draft+склад "
                            f"невалидна — пересоздай через карточку заявки."
                        )
                        logger.info("auto-poll stopped on 404 scoring: rid=%d age=%.0f",
                                    rid, age)
                        return
                    logger.warning("auto-poll API error: %s", e)
                except Exception as e:
                    logger.exception("auto-poll unexpected: %s", e)

            if all_slot_entries:
                if auto_book:
                    # Авто-брон режим: бронируем сами, по 1 слоту на draft.
                    # Берём ПЕРВЫЙ slot каждого draft (самый ранний, отсорт. выше).
                    by_draft: Dict[int, Dict] = {}
                    for s in all_slot_entries:
                        d_id = int(s["draft_id"])
                        if d_id not in by_draft:
                            by_draft[d_id] = s
                    booked_count = 0
                    for d_id, slot in by_draft.items():
                        # drop_off_name из контекста этого draft
                        for d_ctx in drafts:
                            if int(d_ctx.get("draft_id") or 0) == d_id:
                                slot["drop_off_name"] = d_ctx.get("drop_off_warehouse_name") or ""
                                break
                        ok = await _silent_book_slot(bot, chat_id, rid, slot, tg_id or 0)
                        if ok:
                            booked_count += 1
                        await asyncio.sleep(5)  # пауза между supply/create
                    logger.info(
                        "auto-poll auto-book: rid=%d booked %d/%d drafts",
                        rid, booked_count, len(by_draft),
                    )
                    return
                # Legacy режим — присылаем кнопки для ручного выбора
                await _post_found_slots(bot, chat_id, rid, all_slot_entries, tg_id=tg_id or 0)
                logger.info("auto-poll success: rid=%d, slots=%d", rid, len(all_slot_entries))
                return

            if any_ok and not all_slot_entries:
                # API ответил, но реально пусто на эти даты — продолжаем долбить
                # ещё (Ozon может открыть слоты позже), но не чаще раз в 5 мин шлём
                # отчёт. Если за весь час так и не появятся — сработает deadline-финал.
                if attempts % 5 == 0:
                    try:
                        if status_msg_id:
                            await bot.edit_message_text(
                                chat_id=chat_id, message_id=status_msg_id,
                                text=f"🔴 Авто-поиск #{rid}: слотов на выбранные даты пока нет. "
                                     f"Жду открытия (попытка {attempts}/~60).",
                            )
                        else:
                            m = await bot.send_message(
                                chat_id,
                                f"🔴 Авто-поиск #{rid}: слотов на выбранные даты пока нет. "
                                f"Жду открытия (попытка {attempts}/~60).",
                            )
                            status_msg_id = m.message_id
                    except Exception:
                        pass

            # Все 429 — продолжаем. Каждые 5 минут — отчёт.
            if attempts % 5 == 0:
                try:
                    if status_msg_id:
                        await bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=status_msg_id,
                            text=f"♻ Авто-поиск #{rid}: попытка {attempts}, лимит Ozon ещё держит. Жду…",
                        )
                    else:
                        m = await bot.send_message(
                            chat_id,
                            f"♻ Авто-поиск #{rid}: попытка {attempts}, лимит Ozon ещё держит. Жду…",
                        )
                        status_msg_id = m.message_id
                except Exception as e:
                    logger.warning("status update failed: %s", e)

            await asyncio.sleep(interval)

        # Дедлайн — час прошёл, слотов так и нет.
        await bot.send_message(
            chat_id,
            f"😔 Извини, братан — за час Ozon так и не отдал слотов для заявки #{rid}. "
            f"Лимиты упёрлись или у drop-off-точки маршрута в эти кластеры нет. "
            f"Попробуй позже / поменяй drop-off / расширь даты.",
        )
    except asyncio.CancelledError:
        logger.info("auto-poll cancelled: rid=%d", rid)
        raise
    except Exception as e:
        logger.exception("auto-poll fatal: %s", e)
        try:
            await bot.send_message(chat_id, f"❌ Авто-поиск #{rid} упал: <code>{str(e)[:200]}</code>")
        except Exception:
            pass
    finally:
        _AUTO_POLL_TASKS.pop(rid, None)


async def _post_found_slots(
    bot, chat_id: int, rid: int, slots: List[Dict], tg_id: int = 0,
) -> None:
    """Постит найденные слоты пользователю с inline-кнопками.

    Уже забронированные слоты других направлений — выносим в текст-подсказку
    наверху («у тебя уже забронированы Самара 20.05 10:00 …»). На сами кнопки
    ✓ НЕ ставим: раньше сравнивали только по дате → на странице где 20 слотов
    одного дня все получали ✓ и юзеру казалось «всё уже занято». Подсказка
    наверху даёт ту же информацию без шума на кнопках.
    """
    booked_info: List[str] = []
    with db_session() as session:
        req = get_shipment_request(session, rid, user_id=tg_id)
        if req:
            for it in req.items:
                if it.marketplace == "ozon" and it.booked_slot_at:
                    d = it.booked_slot_at
                    booked_info.append(
                        f"{it.cluster} <b>{d.day:02d}.{d.month:02d} {d.hour:02d}:{d.minute:02d}</b>"
                    )

    buttons: List[List[InlineKeyboardButton]] = []
    for i, slot in enumerate(slots[:25]):
        token = f"{rid}_{i}"
        _FOUND_SLOTS[token] = slot
        date_short = (slot.get("from") or "")[:10]
        t_from = (slot.get("from") or "")[11:16]
        wh_short = (slot.get("warehouse_name") or "")[:14]
        btn_text = f"📌 {date_short} {t_from} {wh_short}"
        buttons.append([InlineKeyboardButton(text=btn_text[:40], callback_data=f"obfslot:{token}")])

    buttons.append([InlineKeyboardButton(text="◀ К карточке заявки", callback_data=f"ship_open:{rid}")])

    hint = ""
    if booked_info:
        hint = (
            "\n\n<i>💡 Уже забронировано в этой заявке: "
            + ", ".join(booked_info)
            + ". Подбирай близкое время — один водитель / один рейс.</i>"
        )
    await bot.send_message(
        chat_id,
        f"🎉 <b>Слоты найдены!</b> Заявка #{rid} — {len(slots)} вариантов.\n"
        f"Тапни нужный — бот забронирует его в Ozon ЛК.{hint}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data.startswith("obfslot:"))
async def cb_ob_found_slot_pick(cb: CallbackQuery, state: FSMContext) -> None:
    """Пользователь тапнул слот из auto-poll/auto-walk результата."""
    token = cb.data.split(":", 1)[1]
    slot = _FOUND_SLOTS.get(token)
    if not slot:
        await cb.answer("Слот пропал из кэша. Запусти Ozon-мастер заново.", show_alert=True)
        return

    rid: Optional[int] = None
    try:
        rid = int(token.split("_", 1)[0])
    except (ValueError, IndexError):
        rid = None

    lock_key = f"draft_{slot['draft_id']}"
    if lock_key in _BOOKING_IN_FLIGHT:
        await cb.answer("Бронирование этого слота уже идёт — подожди ответ.", show_alert=True)
        return

    # Немедленный visual feedback: сообщение со слот-кнопками edit'им в
    # «⏳ Бронирую слот …» БЕЗ кнопок. Без этого статусы летят только в
    # сардельку (которая выше в чате) → юзер не видит что клик сработал.
    slot_date = (slot.get("from") or "")[:10]
    slot_time = (slot.get("from") or "")[11:16]
    wh_name = slot.get("warehouse_name") or "—"
    cluster_name = slot.get("cluster") or "?"
    if cb.message:
        try:
            await cb.message.edit_text(
                f"⏳ <b>Бронирую слот</b> · {cluster_name}\n"
                f"📌 {slot_date} {slot_time} · {wh_name}\n\n"
                f"<i>Жди до 1 мин — Ozon обрабатывает supply/create.</i>",
                reply_markup=None,
            )
        except Exception:
            pass  # старое сообщение могло быть уже не editable

    _BOOKING_IN_FLIGHT.add(lock_key)
    try:
        result = await _do_book_slot(cb, slot, rid=rid, state=state)
    finally:
        _BOOKING_IN_FLIGHT.discard(lock_key)

    # Финальный статус — на ту же кнопку-сообщение со слот-пиком, чтобы юзер
    # видел итог ТАМ где кликал (а не только в сардельке наверху). Сразу
    # предлагаем «что дальше»: либо следующее непробронированное направление,
    # либо обратно в карточку. Wizard-lock на rid отпускаем — иначе юзер не
    # сможет перезапустить мастер для остальных кластеров (30-мин TTL).
    if rid is not None:
        _wizard_release(rid)

    if not cb.message:
        return

    status = result.get("status")
    if status == "success":
        order_id = result.get("order_id")
        # Сколько ещё КЛАСТЕРОВ (направлений) не забронированы — раньше считал items
        # (с дублями по SKU в одном кластере), показывал «3 осталось» когда реально 1.
        remaining_clusters_count = 0
        next_mode = "cross" if slot.get("supply_type") == 1 else "direct"
        if rid is not None:
            tg_id_remain = cb.from_user.id if cb.from_user else 0
            with db_session() as session:
                req = get_shipment_request(session, rid, user_id=tg_id_remain)
                if req:
                    remaining_clusters = {
                        it.cluster for it in req.items
                        if it.marketplace == "ozon" and not it.booked_supply_id
                    }
                    remaining_clusters_count = len(remaining_clusters)
                    # Уважим зафиксированный тип заявки, если он есть.
                    if req.ozon_supply_type:
                        next_mode = req.ozon_supply_type
        rows: List[List[InlineKeyboardButton]] = []
        if remaining_clusters_count > 0 and rid is not None:
            # ozon_book_auto — отдельный callback, который сразу запустит Auto-walk
            # минуя scoring picker (юзер: «не понимаю почему опять смотрит scored»).
            rows.append([InlineKeyboardButton(
                text=f"🚀 Бронировать следующее направление ({remaining_clusters_count} осталось)",
                callback_data=f"ozon_book_auto:{rid}:{next_mode}",
            )])
        rows.append([InlineKeyboardButton(
            text="📋 К карточке заявки",
            callback_data=f"ship_open:{rid or 0}",
        )])
        try:
            await cb.message.edit_text(
                f"✅ <b>Слот забронирован!</b>\n"
                f"📦 {cluster_name}\n"
                f"📌 {slot_date} {slot_time} · {wh_name}\n"
                f"🛒 order_id <code>{order_id}</code>",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            )
        except Exception:
            pass
    else:  # fail | timeout | no_cluster
        err = (result.get("error") or status or "неизвестная ошибка")[:300]
        rows = [
            [InlineKeyboardButton(
                text="📋 К карточке заявки",
                callback_data=f"ship_open:{rid or 0}",
            )],
        ]
        try:
            await cb.message.edit_text(
                f"❌ <b>Не получилось забронировать</b>\n"
                f"📦 {cluster_name} · {slot_date} {slot_time}\n\n"
                f"<code>{err}</code>",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            )
        except Exception:
            pass


async def _do_book_slot(
    cb: CallbackQuery, slot: Dict, rid: Optional[int] = None,
    state: Optional[FSMContext] = None,
) -> Dict:
    """Полный flow бронирования через v2: supply/create → polling status → запись в БД.

    Возвращает dict с финальным состоянием — caller сам решает что показать юзеру
    на edit'е сообщения-с-кнопкой (раньше всё шло только в сардельку, юзер не
    видел финальный статус там где кликал):
        `{"status": "success"|"fail"|"timeout"|"no_cluster", "order_id": int|None, "error": str|None}`

    Если передан state — статусы летят в накопительный progress-message; иначе
    отдельными ответами (legacy)."""
    await cb.answer("Бронирую…")
    tg_id = current_user_id_from(cb)
    if tg_id is None:
        return {"status": "fail", "order_id": None, "error": "tg_id missing"}
    oz = _ozon_client_for_tg(tg_id)
    if oz is None:
        if cb.message:
            await cb.message.answer(_NO_OZON_KEYS_MSG)
        return {"status": "fail", "order_id": None, "error": "no ozon creds"}

    async def _say(line: str) -> None:
        if state is not None and cb.message:
            await progress_add(cb.message, state, line)
        elif cb.message:
            await cb.message.answer(line)

    await _say(
        f"⏳ POST /v2/draft/supply/create · draft={slot['draft_id']} · "
        f"cluster={slot.get('cluster_id')} · слот {slot['from'][:16]}–{slot['to'][11:16]}"
    )
    cluster_id = slot.get("cluster_id")
    if not cluster_id:
        await _say("❌ В слоте нет cluster_id (старый кэш). Жми «🔁 Повторить» в карточке.")
        return {"status": "no_cluster", "order_id": None,
                "error": "В слоте нет cluster_id (старый кэш)"}
    try:
        errors = await oz.draft_supply_create_v2(
            draft_id=slot["draft_id"],
            cluster_id=int(cluster_id),
            warehouse_id=slot["warehouse_id"],
            timeslot_from=slot["from"],
            timeslot_to=slot["to"],
            supply_type=slot.get("supply_type", 2),
        )
    except OzonAPIError as e:
        err_s = str(e)[:400]
        await _say(f"❌ {err_s}")
        return {"status": "fail", "order_id": None, "error": err_s}

    if errors:
        err_s = ", ".join(errors[:5])
        await _say(f"❌ Ozon отклонил поставку: <code>{err_s}</code>")
        return {"status": "fail", "order_id": None, "error": err_s}

    await _say("⏳ supply создаётся, polling status…")

    final = None
    for _ in range(30):
        await asyncio.sleep(2)
        try:
            info = await oz.draft_supply_create_status_v2(slot["draft_id"])
        except OzonAPIError as e:
            err_s = str(e)[:200]
            await _say(f"⚠ status: <code>{err_s}</code>")
            return {"status": "fail", "order_id": None, "error": err_s}
        status = str(info.get("status") or "").upper()
        if status in {"SUCCESS", "FAILED"}:
            final = info
            break

    if not final:
        await _say("⚠ Таймаут на финализации supply (но в ЛК может появиться).")
        return {"status": "timeout", "order_id": None,
                "error": "polling timeout (30 × 2с)"}

    status = str(final.get("status") or "?")
    success = status.upper() == "SUCCESS"
    order_id = final.get("order_id")

    if success:
        # Drop-off: для CROSSDOCK имя хаба из slot['drop_off_name'],
        # для DIRECT — имя РФЦ из slot['warehouse_name'].
        drop_off_display = (
            slot.get("drop_off_name")
            or (slot.get("warehouse_name") if slot.get("warehouse_name") and slot.get("warehouse_id") else None)
            or "—"
        )
        await _say(
            f"✅ <b>Поставка создана в Ozon ЛК!</b> order_id <code>{order_id}</code> · "
            f"Кластер {slot['cluster']} · "
            f"Drop-off {drop_off_display} · "
            f"Слот {slot['from'][:16]}–{slot['to'][11:16]}"
        )
        if rid:
            tg_id_book2 = cb.from_user.id if cb.from_user else 0
            with db_session() as session:
                req = get_shipment_request(session, rid, user_id=tg_id_book2)
                if req:
                    if order_id:
                        is_crossdock = _is_crossdock_wh(slot.get("warehouse_name"), slot.get("warehouse_id"))
                        wh_to_save = None if is_crossdock else slot.get("warehouse_name")
                        for it in req.items:
                            if it.marketplace == "ozon" and it.cluster == slot["cluster"]:
                                it.booked_supply_id = str(order_id)
                                it.target_warehouse = wh_to_save
                                it.booked_slot_at = datetime.fromisoformat(
                                    slot["from"].replace("Z", "+00:00").split("+")[0]
                                )
                    from src.services.shipment_service import refresh_request_state_after_booking
                    refresh_request_state_after_booking(req)
            # Помечаем draft как использованный — в кэше не отдадим повторно.
            from src.services.draft_cache import mark_draft_used
            with db_session() as session:
                mark_draft_used(session, int(slot["draft_id"]))
        return {"status": "success", "order_id": order_id, "error": None}
    else:
        errs = final.get("error_reasons") or []
        err_s = ", ".join(str(e) for e in errs[:5])
        await _say(f"❌ status={status}\nerrors: <code>{err_s}</code>")
        return {"status": "fail", "order_id": None,
                "error": f"status={status}; {err_s}"}


@router.callback_query(F.data.startswith("obcancelpoll:"))
async def cb_ob_cancel_poll(cb: CallbackQuery) -> None:
    """Остановить фоновый auto-poll."""
    try:
        rid = int(cb.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await cb.answer("Битый callback", show_alert=True)
        return
    task = _AUTO_POLL_TASKS.pop(rid, None)
    if task and not task.done():
        task.cancel()
        await cb.answer("Авто-поиск остановлен")
        if cb.message:
            # edit прямо на «⚠ Не удалось получить слоты» — не плодим хвост.
            rows = [[InlineKeyboardButton(text="◀ К карточке заявки",
                                          callback_data=f"ship_open:{rid}")]]
            await safe_edit_or_answer(
                cb.message,
                f"✖ Авто-поиск для заявки #{rid} остановлен.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            )
    else:
        await cb.answer("Авто-поиск уже не активен")


@router.callback_query(F.data.startswith("obretry:"))
async def cb_ob_retry(cb: CallbackQuery, state: FSMContext) -> None:
    """Повторить поиск слотов по уже созданным drafts (без пересоздания)."""
    await cb.answer("Повторяю поиск слотов…")
    data = await state.get_data()
    if not data.get("ob_drafts"):
        if cb.message:
            await cb.message.answer(
                "⚠ Данные о drafts в этом состоянии пропали. "
                "Открой карточку заявки и нажми «🚀 Создать поставку Ozon» — "
                "если drafts свежие (<30 мин), новые draft не создаст."
            )
        return
    if cb.message:
        await _fetch_slots_for_drafts(cb.message, state)


# ── Новый picker: один edit-message, выбор по очереди, в конце bulk-book ─

SLOTS_PER_PAGE = 15


def _is_crossdock_wh(name: Optional[str], wh_id) -> bool:
    """True если slot — CROSSDOCK (warehouse_id=0 или пустой/служебный)."""
    if not name or name in ("—", "#0"):
        return True
    try:
        return int(wh_id) == 0
    except (TypeError, ValueError):
        return False


async def _render_date_overview(msg: Message, state: FSMContext) -> None:
    """Сводный обзорный экран: по каждой дате показывает «M/N кластеров доступны»
    + кнопки авто-подбора на конкретную дату + кнопку ручного выбора.

    Под капотом строит date_map = {date_iso → set(cluster_names)}, сортирует и
    рендерит. Failed-кластеры (без слотов вообще) показывает отдельно.
    """
    data = await state.get_data()
    clusters: List[Dict] = data.get("ob_picker_clusters") or []
    failed_names: List[str] = list(data.get("ob_failed_clusters") or [])
    # Кластеры, отбитые scoring'ом (мы их потеряли ещё до timeslot/info), тоже
    # учитываем — иначе total = только живые, и юзер видит «2/2» когда реально 2/5.
    failed_scoring_names: List[str] = list(data.get("ob_failed_clusters_scoring") or [])
    for n in failed_scoring_names:
        if n not in failed_names:
            failed_names.append(n)
    msg_id = data.get("ob_overview_msg_id")
    rid = data.get("ob_rid")

    if not clusters:
        # Нечего показывать — fallback к picker (он сам handle'нет пустое).
        await _render_picker_panel(msg, state)
        return

    # date_iso → set(cluster_names) с хотя бы одним слотом в эту дату
    date_map: Dict[str, set] = {}
    for c in clusters:
        cname = c["cluster"]
        dates = {s["from"][:10] for s in c.get("slots") or []}
        for d in dates:
            date_map.setdefault(d, set()).add(cname)

    # total = ВСЕ кластеры в скоупе заявки (включая отбитые scoring'ом и timeslot/info),
    # чтобы бар честно показывал «2 из 5» а не «2 из 2». Источник — ob_clusters,
    # выставлен в `_start_ozon_book_wizard`.
    all_clusters: List[str] = data.get("ob_clusters") or [c["cluster"] for c in clusters]
    total = len(all_clusters)
    sorted_dates = sorted(date_map.keys())

    lines: List[str] = [f"📅 <b>Сводка по датам</b> · заявка #{rid or '?'}", ""]
    lines.append(f"Кластеров со слотами: <b>{len(clusters)}/{total}</b>")
    if failed_names:
        lines.append(
            f"⚠ Без слотов ({len(failed_names)}): " + ", ".join(failed_names)
        )
        lines.append("   <i>смени drop-off через карточку заявки → Ozon CROSSDOCK</i>")
    lines.append("")

    bar_w = 5  # эмодзи-квадраты шире текстовых — сократили
    for d in sorted_dates:
        avail = len(date_map[d])
        filled = round(bar_w * avail / total)
        # Зелёный/белый квадраты вместо ░/█ — на тёмной теме «░» выглядит как
        # странный белый прямоугольник, эмодзи читаются однозначно.
        bar = "🟩" * filled + "⬜" * (bar_w - filled)
        d_short = f"{d[8:10]}.{d[5:7]}"
        # Список кластеров если их немного, иначе только счётчик
        cl_names = sorted(date_map[d])
        if len(cl_names) <= 4:
            tail = " — " + ", ".join(cl_names)
        else:
            tail = ""
        lines.append(f"<code>{d_short}</code> {bar} {avail}/{total}{tail}")

    lines.append("")
    lines.append("Выбери дату → бот возьмёт <b>последний</b> слот этого дня для каждого кластера (запас времени на упаковку).")
    lines.append("Или жми «🎯 Выбрать слоты вручную» — откроется детальный picker.")

    text = "\n".join(lines)

    rows: List[List[InlineKeyboardButton]] = []
    # Кнопки авто-подбора: даты с покрытием ≥1, сортируем по убыванию покрытия,
    # потом по дате. Максимум 6 кнопок чтоб не загромождать.
    sorted_for_buttons = sorted(
        sorted_dates,
        key=lambda d: (-len(date_map[d]), d),
    )[:6]
    for d in sorted_for_buttons:
        avail = len(date_map[d])
        d_short = f"{d[8:10]}.{d[5:7]}"
        rows.append([InlineKeyboardButton(
            text=f"🚀 Все на {d_short} ({avail}/{total})",
            callback_data=f"obauto:{d}",
        )])

    rows.append([InlineKeyboardButton(
        text="🎯 Выбрать слоты вручную", callback_data="obmanual",
    )])
    rows.append([InlineKeyboardButton(text="✖ Отмена", callback_data="obpcancel")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)

    if msg_id:
        try:
            await msg.bot.edit_message_text(
                text, chat_id=msg.chat.id, message_id=msg_id, reply_markup=kb,
            )
            return
        except Exception as e:
            logger.warning("overview edit failed: %s — sending new", e)
    m = await msg.answer(text, reply_markup=kb)
    await state.update_data(ob_overview_msg_id=m.message_id, ob_picker_msg_id=m.message_id)


async def _render_picker_panel(msg: Message, state: FSMContext) -> None:
    """Рендерит/редактирует ОДНУ панель выбора слотов.

    Показывает слоты текущего кластера (ob_picker_idx) с pagination.
    Юзер тапает слот → сохраняем выбор → idx+=1 → re-render.
    Когда все кластеры покрыты — показываем confirm-экран.
    """
    data = await state.get_data()
    clusters: List[Dict] = data.get("ob_picker_clusters") or []
    idx: int = int(data.get("ob_picker_idx") or 0)
    page: int = int(data.get("ob_picker_page") or 0)
    choices: Dict[str, Dict] = data.get("ob_picker_choices") or {}
    msg_id = data.get("ob_picker_msg_id")
    rid = data.get("ob_rid")

    if not clusters:
        await msg.answer("⚠ Нет данных для выбора слотов.")
        return

    if idx >= len(clusters):
        await _render_confirm_panel(msg, state)
        return

    cur = clusters[idx]
    slots = cur["slots"]
    pages_total = max(1, (len(slots) + SLOTS_PER_PAGE - 1) // SLOTS_PER_PAGE)
    if page >= pages_total:
        page = pages_total - 1
        await state.update_data(ob_picker_page=page)
    page_slots = slots[page * SLOTS_PER_PAGE : (page + 1) * SLOTS_PER_PAGE]

    lines: List[str] = []
    lines.append(f"🎯 <b>Выбор таймслотов</b> · заявка #{rid or '?'}")
    drop_off = cur.get("drop_off_name") or "—"
    lines.append("")
    lines.append(
        f"[{idx + 1}/{len(clusters)}] 🟡 <b>{cur['cluster']}</b>"
    )
    lines.append(
        f"Drop-off: <i>{drop_off}</i> · слотов: {len(slots)} · стр {page + 1}/{pages_total}"
    )
    lines.append("Тапни слот ⬇")
    lines.append("")
    lines.append("<b>Прогресс:</b>")
    for i, c in enumerate(clusters):
        ch = choices.get(str(i))
        if ch:
            d_short = f"{ch['from'][8:10]}.{ch['from'][5:7]}"
            t_short = ch["from"][11:16]
            lines.append(f"  ✅ {c['cluster']} — {d_short} {t_short}")
        elif i == idx:
            lines.append(f"  🟡 {c['cluster']} — выбираю…")
        else:
            lines.append(f"  ⏳ {c['cluster']}")

    text = "\n".join(lines)

    rows: List[List[InlineKeyboardButton]] = []
    for offset, s in enumerate(page_slots):
        global_sn = page * SLOTS_PER_PAGE + offset
        d_short = f"{s['from'][8:10]}.{s['from'][5:7]}"
        t_short = s["from"][11:16]
        if _is_crossdock_wh(s.get("warehouse_name"), s.get("warehouse_id")):
            label = f"{d_short} {t_short}"
        else:
            wh_short = (s.get("warehouse_name") or "")[:14]
            label = f"{d_short} {t_short} · {wh_short}"
        rows.append([InlineKeyboardButton(
            text=label[:40], callback_data=f"obps:{idx}:{global_sn}",
        )])

    nav: List[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀ Назад", callback_data=f"obpg:{idx}:{page - 1}"))
    if page < pages_total - 1:
        nav.append(InlineKeyboardButton(text="Вперёд ▶", callback_data=f"obpg:{idx}:{page + 1}"))
    if nav:
        rows.append(nav)

    if idx > 0:
        prev_cl = clusters[idx - 1]["cluster"]
        rows.append([InlineKeyboardButton(
            text=f"↩ Перевыбрать «{prev_cl}»",
            callback_data=f"obpback:{idx - 1}",
        )])

    rows.append([InlineKeyboardButton(text="✖ Отмена", callback_data="obpcancel")])

    kb = InlineKeyboardMarkup(inline_keyboard=rows)

    if msg_id:
        try:
            await msg.bot.edit_message_text(
                text, chat_id=msg.chat.id, message_id=msg_id, reply_markup=kb,
            )
            return
        except Exception as e:
            logger.warning("picker edit failed: %s — sending new", e)

    m = await msg.answer(text, reply_markup=kb)
    await state.update_data(ob_picker_msg_id=m.message_id)


async def _render_confirm_panel(msg: Message, state: FSMContext) -> None:
    """Финальный экран: сводка всех выборов + кнопка «🚀 Забронировать всё»."""
    data = await state.get_data()
    clusters: List[Dict] = data.get("ob_picker_clusters") or []
    choices: Dict[str, Dict] = data.get("ob_picker_choices") or {}
    msg_id = data.get("ob_picker_msg_id")
    rid = data.get("ob_rid")

    lines: List[str] = [f"🎯 <b>Сводка выборов</b> · заявка #{rid or '?'}", ""]
    for i, c in enumerate(clusters):
        ch = choices.get(str(i))
        if ch:
            d_short = f"{ch['from'][8:10]}.{ch['from'][5:7]}"
            t_short = ch["from"][11:16]
            if _is_crossdock_wh(ch.get("warehouse_name"), ch.get("warehouse_id")):
                # CROSSDOCK: РФЦ определит Ozon — не показываем заглушку «#0».
                lines.append(f"  ✅ <b>{c['cluster']}</b> — {d_short} {t_short}")
            else:
                wh = ch.get("warehouse_name") or "—"
                lines.append(f"  ✅ <b>{c['cluster']}</b> — {d_short} {t_short} → <i>{wh}</i>")
        else:
            lines.append(f"  ⚠ <b>{c['cluster']}</b> — не выбран")
    lines.append("")
    lines.append("Жми «🚀 Забронировать всё» — бот по очереди создаст поставки в Ozon ЛК.")

    text = "\n".join(lines)
    rows = [
        [InlineKeyboardButton(text="🚀 Забронировать всё", callback_data="obpconfirm")],
    ]
    if clusters:
        last_cl = clusters[-1]["cluster"]
        rows.append([InlineKeyboardButton(
            text=f"↩ Перевыбрать «{last_cl}»",
            callback_data=f"obpback:{len(clusters) - 1}",
        )])
    rows.append([InlineKeyboardButton(text="✖ Отмена", callback_data="obpcancel")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)

    if msg_id:
        try:
            await msg.bot.edit_message_text(
                text, chat_id=msg.chat.id, message_id=msg_id, reply_markup=kb,
            )
            return
        except Exception as e:
            logger.warning("confirm edit failed: %s", e)
    m = await msg.answer(text, reply_markup=kb)
    await state.update_data(ob_picker_msg_id=m.message_id)


@router.callback_query(OzonBook.pick_slot, F.data == "obmanual")
async def cb_ob_overview_manual(cb: CallbackQuery, state: FSMContext) -> None:
    """С обзорного экрана → ручной picker."""
    await cb.answer()
    if cb.message:
        # Picker будет edit'ить то же сообщение что и overview (ob_picker_msg_id).
        await _render_picker_panel(cb.message, state)


@router.callback_query(OzonBook.pick_slot, F.data.startswith("obauto:"))
async def cb_ob_overview_auto(cb: CallbackQuery, state: FSMContext) -> None:
    """Авто-подбор на дату: для каждого кластера берём ПОСЛЕДНИЙ слот в эту
    дату (даём максимум времени на упаковку); кластеры без слотов в этот
    день пропускаем. Затем сразу confirm-panel."""
    parts = cb.data.split(":", 1)
    if len(parts) != 2 or not cb.message:
        await cb.answer("Битый callback", show_alert=True)
        return
    target_date = parts[1]  # YYYY-MM-DD
    await cb.answer(f"Подбираю на {target_date[8:10]}.{target_date[5:7]}…")
    data = await state.get_data()
    clusters: List[Dict] = data.get("ob_picker_clusters") or []
    choices: Dict[str, Dict] = {}
    skipped: List[str] = []
    for i, c in enumerate(clusters):
        same_day = [s for s in (c.get("slots") or []) if s["from"][:10] == target_date]
        if not same_day:
            skipped.append(c["cluster"])
            continue
        same_day.sort(key=lambda s: s["from"])
        chosen = same_day[-1]  # последний — самый поздний час дня (запас на упаковку)
        choices[str(i)] = {
            "cluster": c["cluster"],
            "cluster_id": c.get("cluster_id"),
            "draft_id": c["draft_id"],
            "supply_type": c.get("supply_type", 2),
            "drop_off_name": c.get("drop_off_name"),
            "warehouse_id": chosen["warehouse_id"],
            "warehouse_name": chosen["warehouse_name"],
            "from": chosen["from"],
            "to": chosen["to"],
        }
    if not choices:
        await cb.message.answer(
            f"⚠ На дату {target_date} нет ни одного доступного кластера — попробуй другую."
        )
        return
    # idx ставим за пределы — _render_confirm_panel в picker сразу нарисует summary
    await state.update_data(
        ob_picker_choices=choices,
        ob_picker_idx=len(clusters),
    )
    if skipped:
        await cb.message.answer(
            f"ℹ Пропустил кластеры без слотов на {target_date}: {', '.join(skipped)}"
        )
    await _render_confirm_panel(cb.message, state)


@router.callback_query(OzonBook.pick_slot, F.data.startswith("obps:"))
async def cb_ob_picker_pick(cb: CallbackQuery, state: FSMContext) -> None:
    """Тап по слоту — запоминаем, переходим к следующему кластеру."""
    parts = cb.data.split(":")
    if len(parts) != 3:
        await cb.answer("Битый callback", show_alert=True)
        return
    try:
        idx = int(parts[1])
        global_sn = int(parts[2])
    except ValueError:
        await cb.answer("Битый callback", show_alert=True)
        return
    data = await state.get_data()
    clusters: List[Dict] = data.get("ob_picker_clusters") or []
    if idx >= len(clusters):
        await cb.answer("Неверный индекс кластера", show_alert=True)
        return
    slots = clusters[idx]["slots"]
    if global_sn >= len(slots):
        await cb.answer("Слот вне диапазона", show_alert=True)
        return
    s = slots[global_sn]
    cur = clusters[idx]
    choice = {
        "draft_id": cur["draft_id"],
        "cluster_id": cur.get("cluster_id"),
        "supply_type": cur.get("supply_type", 2),
        "warehouse_id": s["warehouse_id"],
        "warehouse_name": s["warehouse_name"],
        "drop_off_name": cur.get("drop_off_name") or "",
        "from": s["from"],
        "to": s["to"],
        "cluster": cur["cluster"],
    }
    choices = dict(data.get("ob_picker_choices") or {})
    choices[str(idx)] = choice
    await state.update_data(
        ob_picker_choices=choices,
        ob_picker_idx=idx + 1,
        ob_picker_page=0,
    )
    await cb.answer(f"✓ {cur['cluster']}: {s['from'][8:10]}.{s['from'][5:7]} {s['from'][11:16]}")
    if cb.message:
        await _render_picker_panel(cb.message, state)


@router.callback_query(OzonBook.pick_slot, F.data.startswith("obpg:"))
async def cb_ob_picker_page(cb: CallbackQuery, state: FSMContext) -> None:
    """Пагинация внутри текущего кластера."""
    parts = cb.data.split(":")
    if len(parts) != 3:
        await cb.answer()
        return
    try:
        idx = int(parts[1])
        page = int(parts[2])
    except ValueError:
        await cb.answer()
        return
    data = await state.get_data()
    cur_idx = int(data.get("ob_picker_idx") or 0)
    if idx != cur_idx:
        await cb.answer("Этот кластер уже не активен — выбираем другой.", show_alert=True)
        return
    await state.update_data(ob_picker_page=page)
    await cb.answer()
    if cb.message:
        await _render_picker_panel(cb.message, state)


@router.callback_query(OzonBook.pick_slot, F.data.startswith("obpback:"))
async def cb_ob_picker_back(cb: CallbackQuery, state: FSMContext) -> None:
    """Сброс выбора с target_idx и далее — перевыбор."""
    try:
        target_idx = int(cb.data.split(":", 1)[1])
    except ValueError:
        await cb.answer()
        return
    data = await state.get_data()
    choices = {
        k: v for k, v in (data.get("ob_picker_choices") or {}).items()
        if int(k) < target_idx
    }
    await state.update_data(
        ob_picker_choices=choices,
        ob_picker_idx=target_idx,
        ob_picker_page=0,
    )
    await cb.answer("Перевыбираем…")
    if cb.message:
        await _render_picker_panel(cb.message, state)


@router.callback_query(OzonBook.pick_slot, F.data == "obpcancel")
async def cb_ob_picker_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer("Отменено")
    data = await state.get_data()
    msg_id = data.get("ob_picker_msg_id")
    rid = data.get("ob_rid")
    if msg_id and cb.message:
        try:
            await cb.bot.edit_message_text(
                "✖ Выбор слотов отменён.",
                chat_id=cb.message.chat.id,
                message_id=msg_id,
            )
        except Exception:
            pass
    if rid:
        _wizard_release(int(rid))
    await state.clear()


@router.callback_query(OzonBook.pick_slot, F.data == "obpconfirm")
async def cb_ob_picker_confirm(cb: CallbackQuery, state: FSMContext) -> None:
    """Bulk-book всех выбранных слотов — тонкая обёртка над `_run_bulk_book`."""
    await cb.answer("Бронирую все…")
    if not cb.message:
        return
    await _run_bulk_book(cb.bot, cb.message, state)


async def _run_bulk_book(bot, msg: Message, state: FSMContext) -> None:
    """Запускает bulk-booking по `ob_picker_choices` из state. Зовётся:
    1) Из `cb_ob_picker_confirm` после ручного выбора слотов в picker'е;
    2) Из auto-book режима (юзер выбрал часы → бот сам подобрал первый слот
       в окне для каждого кластера и сразу бронирует — без слот-пикера)."""
    data = await state.get_data()
    clusters: List[Dict] = data.get("ob_picker_clusters") or []
    choices: Dict[str, Dict] = data.get("ob_picker_choices") or {}
    rid = data.get("ob_rid")
    msg_id = data.get("ob_picker_msg_id") or data.get("ob_progress_msg_id")
    if not choices:
        await msg.answer("⚠ Нечего бронировать — ни одного слота не выбрано.")
        return

    # Превращаем picker-panel в running-статус (он же станет «сарделькой» брони).
    not_fit_pre = data.get("ob_failed_clusters") or []
    n_book = len(choices)
    n_total_card = n_book + len(not_fit_pre)
    if not_fit_pre:
        header = (
            f"⚙ <b>Bulk-бронирование</b> заявки #{rid or '?'} — "
            f"{n_book} из {n_total_card} (ещё {len(not_fit_pre)} в авто-поиске)"
        )
    else:
        header = f"⚙ <b>Bulk-бронирование</b> заявки #{rid or '?'} — {n_book} поставок"
    try:
        if msg_id:
            await bot.edit_message_text(
                header,
                chat_id=msg.chat.id,
                message_id=msg_id,
            )
            await state.update_data(
                ob_progress_msg_id=msg_id, ob_progress_text=header,
                ob_picker_msg_id=None,
            )
        else:
            await progress_start(msg, state, header)
    except Exception as e:
        logger.warning("bulk header edit failed: %s", e)
        await progress_start(msg, state, header)

    ok_count = 0
    fail_count = 0
    summary: List[Tuple[str, str]] = []
    # Ozon /v2/draft/supply/create лимит ~2 req/sec на кабинет. Драфты уже
    # созданы — нужно только бронирование. Пауза 5с — буфер на anti-spam,
    # если поймаем 429 — _book_one_slot retry с паузой 45с (внутри).
    pause_between = 5
    pause_after_429 = 30
    keys = sorted(choices.keys(), key=int)
    last_was_429 = False
    for idx, i_str in enumerate(keys):
        slot = choices[i_str]
        if idx > 0:
            wait_s = pause_after_429 if last_was_429 else pause_between
            reason = (
                "после 429 — даём Ozon отдохнуть"
                if last_was_429
                else "rate-limit /v2/draft/supply/create"
            )
            await progress_add(
                msg, state,
                f"\n⏱ Пауза {wait_s}с до следующего кластера ({reason})…",
            )
            await asyncio.sleep(wait_s)
        await progress_add(
            msg, state,
            f"\n⏳ <b>{slot['cluster']}</b>: draft {slot['draft_id']} → "
            f"{slot['from'][:16]}–{slot['to'][11:16]}",
        )
        lock_key = f"draft_{slot['draft_id']}"
        if lock_key in _BOOKING_IN_FLIGHT:
            await progress_add(msg, state, "  ⚠ Параллельная бронь уже идёт — пропуск.")
            summary.append((slot["cluster"], "⚠"))
            last_was_429 = False
            continue
        _BOOKING_IN_FLIGHT.add(lock_key)
        try:
            ok, was_429 = await _book_one_slot(bot, msg, state, slot, rid)
        except Exception as e:
            logger.exception("bulk book exception: %s", e)
            await progress_add(msg, state, f"  ❌ Exception: <code>{str(e)[:200]}</code>")
            ok = False
            was_429 = False
        finally:
            _BOOKING_IN_FLIGHT.discard(lock_key)
        last_was_429 = was_429
        if ok:
            ok_count += 1
            summary.append((slot["cluster"], "✅"))
        else:
            fail_count += 1
            summary.append((slot["cluster"], "❌"))

    final_lines = "\n".join(f"  {mark} {cl}" for cl, mark in summary)
    # Failed-кластеры до bulk-book (отказ scoring: OUT_OF_ASSORTMENT, NO_TIMESLOTS).
    # Раньше они не попадали в финальный отчёт → юзер видел «Успешно: 1»
    # при реально 1 из 3 кластеров.
    failed_scoring = data.get("ob_failed_clusters_scoring") or []
    failed_other = data.get("ob_failed_clusters") or []
    out_of_book = [c for c in failed_other if c not in failed_scoring]
    skipped_block = ""
    n_total = ok_count + fail_count + len(failed_scoring) + len(out_of_book)
    if failed_scoring:
        skipped_block += (
            f"\n\n🚫 <b>Без слотов на твои даты</b> ({len(failed_scoring)}):\n  "
            + ", ".join(failed_scoring)
        )
    if out_of_book:
        skipped_block += (
            f"\n\n⏳ <b>В авто-поиске</b> ({len(out_of_book)}): "
            + ", ".join(out_of_book)
            + "\n  <i>Бот ищет слоты в течение часа, пришлёт сообщение при успехе.</i>"
        )
    await progress_add(
        msg, state,
        f"\n🏁 <b>Готово.</b> Из {n_total} направлений: "
        f"✅ {ok_count} · ❌ {fail_count}"
        + (f" · 🚫 {len(failed_scoring)} без слотов" if failed_scoring else "")
        + (f" · ⏳ {len(out_of_book)} ищу" if out_of_book else "")
        + f"\n{final_lines}{skipped_block}",
    )

    if rid:
        rows: List[List[InlineKeyboardButton]] = []
        if fail_count > 0 or failed_scoring:
            ob_type = data.get("ob_type") or "CREATE_TYPE_DIRECT"
            resume_mode = "cross" if "CROSSDOCK" in ob_type else "direct"
            remain = fail_count + len(failed_scoring)
            rows.append([InlineKeyboardButton(
                text=f"🔁 Продолжить с оставшимися ({remain})",
                callback_data=f"ozon_book_card:{rid}:{resume_mode}",
            )])
        rows.append([InlineKeyboardButton(
            text="📋 К карточке заявки", callback_data=f"ship_open:{rid}",
        )])
        if ok_count > 0:
            rows.append([InlineKeyboardButton(
                text="📤 Скачать ТЗ Отгрузки", callback_data=f"ship_tz:{rid}",
            )])
        rows.append([InlineKeyboardButton(
            text="🌐 Ozon ЛК → Поставки",
            url="https://seller.ozon.ru/app/supply-orders",
        )])
        n_polling = len(out_of_book)
        if fail_count == 0 and not failed_scoring and not n_polling:
            header = f"✅ Все {ok_count} забронированы. Что дальше?"
        elif fail_count == 0 and not failed_scoring and n_polling:
            header = (
                f"✅ Забронировано {ok_count}/{n_total} · "
                f"⏳ {n_polling} ищу слот ещё час\n"
                f"<i>Не нашлось — пришлю сообщение через час.</i>"
            )
        else:
            header = (
                f"⚠ Забронировано {ok_count}/{n_total}. "
                f"Не удалось: {fail_count + len(failed_scoring)}."
            )
            if n_polling:
                header += f"\n⏳ Ещё {n_polling} в авто-поиске на час."
        try:
            await msg.answer(header, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        except Exception:
            pass
        _wizard_release(int(rid))
    await state.clear()


async def _book_one_slot(bot, msg, state, slot: Dict, rid: Optional[int]) -> Tuple[bool, bool]:
    """Booking одного слота. Возвращает (ok, was_429_on_supply_create).
    Пишет статусы в сардельку через progress_add.

    Retry-логика на 429: до 2 повторов с паузой 45 сек (Ozon /v2/draft/supply/create
    имеет per-second лимит — при bulk-bookинге легко словить даже с паузами между
    вызовами). was_429=True означает что в этой брони был хотя бы один 429 на
    supply/create — bulk-loop использует это чтобы увеличить паузу до следующего
    кластера.
    """
    oz = await _ozon_client_from_state(state)
    if oz is None:
        await progress_add(msg, state, f"  ❌ {_NO_OZON_KEYS_MSG}")
        return False, False
    cluster_id = slot.get("cluster_id")
    if not cluster_id:
        await progress_add(msg, state, "  ❌ Нет cluster_id — пропуск.")
        return False, False

    errors = None
    max_attempts = 3
    saw_429 = False
    for attempt in range(1, max_attempts + 1):
        try:
            errors = await oz.draft_supply_create_v2(
                draft_id=slot["draft_id"],
                cluster_id=int(cluster_id),
                warehouse_id=slot["warehouse_id"],
                timeslot_from=slot["from"],
                timeslot_to=slot["to"],
                supply_type=slot.get("supply_type", 2),
            )
            break
        except OzonAPIError as e:
            err_str = str(e)
            is_429 = "429" in err_str or "rate limit" in err_str.lower()
            if is_429:
                saw_429 = True
            if is_429 and attempt < max_attempts:
                await progress_add(
                    msg, state,
                    f"  ⏸ Ozon 429 (попытка {attempt}/{max_attempts}) — жду 60с…",
                )
                await asyncio.sleep(60)
                continue
            await progress_add(msg, state, f"  ❌ supply/create: <code>{err_str[:200]}</code>")
            return False, saw_429
    if errors:
        await progress_add(msg, state, f"  ❌ Ozon отклонил: <code>{', '.join(errors[:5])}</code>")
        return False, saw_429
    await progress_add(msg, state, "  ⏳ polling status…")
    final = None
    for _ in range(30):
        await asyncio.sleep(2)
        try:
            info = await oz.draft_supply_create_status_v2(slot["draft_id"])
        except OzonAPIError as e:
            await progress_add(msg, state, f"  ⚠ status: <code>{str(e)[:200]}</code>")
            return False, saw_429
        status = str(info.get("status") or "").upper()
        if status in {"SUCCESS", "FAILED"}:
            final = info
            break
    if not final:
        await progress_add(msg, state, "  ⚠ Таймаут — supply возможно ещё создаётся в Ozon.")
        return False, saw_429
    status = str(final.get("status") or "?")
    if status.upper() != "SUCCESS":
        errs = final.get("error_reasons") or []
        err_s = ", ".join(str(e) for e in errs[:5])
        await progress_add(msg, state, f"  ❌ status={status} · {err_s}")
        return False, saw_429
    order_id = final.get("order_id")
    drop_off_display = slot.get("drop_off_name") or slot.get("warehouse_name") or "—"
    await progress_add(
        msg, state,
        f"  ✅ order_id <code>{order_id}</code> · drop-off {drop_off_display}",
    )
    if rid:
        tg_id_book3 = int((await state.get_data()).get("ob_tg_id") or 0)
        with db_session() as session:
            req = get_shipment_request(session, rid, user_id=tg_id_book3)
            if req and order_id:
                is_crossdock = _is_crossdock_wh(slot.get("warehouse_name"), slot.get("warehouse_id"))
                wh_to_save = None if is_crossdock else slot.get("warehouse_name")
                for it in req.items:
                    if it.marketplace == "ozon" and it.cluster == slot["cluster"]:
                        it.booked_supply_id = str(order_id)
                        it.target_warehouse = wh_to_save
                        it.booked_slot_at = datetime.fromisoformat(
                            slot["from"].replace("Z", "+00:00").split("+")[0]
                        )
                from src.services.shipment_service import refresh_request_state_after_booking
                refresh_request_state_after_booking(req)
        from src.services.draft_cache import mark_draft_used
        with db_session() as session:
            mark_draft_used(session, int(slot["draft_id"]))
    return True, saw_429


# ── CROSSDOCK: выбор drop-off-точки для каждого кластера ─────────────────


async def _ask_dropoff_for_next_cluster(msg: Message, state: FSMContext) -> None:
    """Спросить drop-off-точку для текущего ob_cluster_idx кластера.
    Если выбран флаг "одна точка для всех" — спросим один раз и заполним
    choices для всех кластеров. Если все кластеры выбраны — переходим к draft_create."""
    data = await state.get_data()
    rid = data.get("ob_rid")
    clusters: List[str] = data.get("ob_clusters") or []
    idx: int = int(data.get("ob_cluster_idx") or 0)
    choices: Dict = data.get("ob_dropoff_choices") or {}

    # Защита от orphan callback'а (state очистился после /start, /cancel или
    # после завершения предыдущего wizard'а). Юзер тапает старую кнопку drop-off
    # picker'а → state пустой → раньше попадали в «idx>=len» с пустыми clusters
    # → шло «Drop-off-точки выбраны для 0 кластеров» + KeyError'ы дальше.
    if rid is None or not clusters:
        logger.info("ask_dropoff: stale callback (rid=%s, clusters=%s) — skip",
                    rid, len(clusters))
        return

    if idx >= len(clusters):
        # Защита от повторного захода — юзер кликает «◀ К выбору» / пагинацию /
        # старые кнопки уже после того как drafts начали создаваться. Без этого
        # в сардельку дублировался блок «✅ Drop-off-точки выбраны» и стартовал
        # параллельный поток `_create_drafts_and_fetch_scoring` (lock внутри его
        # глушит, но мусор в сардельке остаётся).
        if rid is not None and (rid in _DRAFTS_CREATING or data.get("ob_drafts")):
            logger.info("dropoff_complete re-entry skipped rid=%s "
                        "(creating=%s, has_drafts=%s)",
                        rid, rid in _DRAFTS_CREATING, bool(data.get("ob_drafts")))
            return

        # Замещаем picker-сообщение на финальный confirm без кнопок —
        # пагинация хабов / «◀ К выбору» / старые фавориты больше не кликабельны,
        # параллельный поток уже физически невозможен (а не только заглушен lock'ом).
        choices_block = "\n".join(
            f"  • <b>{v.get('name')}</b> → «{c}»"
            for c, v in choices.items()
        )
        try:
            await msg.edit_text(
                f"✅ <b>Точки отгрузки выбраны</b>:\n{choices_block}",
                reply_markup=None,
            )
        except Exception:
            pass  # picker мог уже не быть editable — не критично

        # Сохраняем drop-off в БД — нужен для авто-брон CROSSDOCK
        # (`/v1/draft/crossdock/create` требует drop_off_point_warehouse_id).
        # До этого правила drop-off жил только в state и пропадал после wizard'а.
        tg_id_dropoff = int(data.get("ob_tg_id") or 0)
        try:
            with db_session() as _s:
                _req = get_shipment_request(_s, rid, user_id=tg_id_dropoff)
                if _req:
                    cd_map = dict(_req.crossdock_warehouses_json or {})
                    for c, v in choices.items():
                        try:
                            cd_map[c] = int(v.get("wh_id") or 0)
                        except (TypeError, ValueError):
                            pass
                    _req.crossdock_warehouses_json = cd_map
        except Exception as e:
            logger.warning("save crossdock_warehouses_json failed rid=%s: %s", rid, e)

        # В «сардельку» — кратко: детали уже выше, в самом picker'е.
        await progress_add(
            msg, state,
            f"\n✅ Drop-off-точки выбраны для {len(clusters)} кластер"
            f"{'ов' if len(clusters) != 1 else 'а'}.",
        )
        await _create_drafts_and_fetch_scoring(msg, state)
        return

    cluster = clusters[idx]
    # Грузим любимые точки
    from src.db.models import FavoriteCrossdockPoint
    with db_session() as session:
        favs = (
            session.query(FavoriteCrossdockPoint)
            .order_by(
                FavoriteCrossdockPoint.use_count.desc(),
                FavoriteCrossdockPoint.last_used_at.desc(),
                FavoriteCrossdockPoint.created_at.desc(),
            )
            .all()
        )
        fav_list = [
            {"id": f.id, "name": f.name, "wh_id": f.warehouse_id, "type": f.point_type}
            for f in favs
        ]

    rows: List[List[InlineKeyboardButton]] = []
    if fav_list:
        for f in fav_list[:8]:
            from src.bot.handlers.favorites import _type_label
            t = _type_label(f.get("type") or "")
            label = f"⭐ {f['name'][:30]} · {t}"[:62]
            rows.append([InlineKeyboardButton(
                text=label, callback_data=f"obdo:fav:{f['wh_id']}",
            )])
    rows.append([InlineKeyboardButton(
        text="🏭 Все доступные хабы (поиск)", callback_data="obdo:all",
    )])
    rows.append([InlineKeyboardButton(
        text="✏ Ввести имя точки", callback_data="obdo:input",
    )])
    rows.append([InlineKeyboardButton(text="✖ Отмена", callback_data="cancel")])

    # Для multi-cluster заявок предлагаем «одна точка на все» — на первом шаге.
    apply_all = data.get("ob_dropoff_apply_all", False)
    apply_all_hint = ""
    if len(clusters) > 1 and idx == 0 and not apply_all:
        rows.insert(0, [InlineKeyboardButton(
            text="🔁 Использовать одну точку для всех кластеров",
            callback_data="obdo:apply_all_toggle",
        )])
        apply_all_hint = (
            f"\n\n<i>💡 У тебя {len(clusters)} кластеров в заявке. Жми «Использовать "
            f"одну точку для всех», чтобы выбрать drop-off один раз — применится ко всем.</i>"
        )
    elif apply_all and idx == 0:
        apply_all_hint = (
            f"\n\n<i>🔁 Режим «одна точка на все {len(clusters)} кластеров» включён. "
            f"Тапни точку — применится ко всем сразу.</i>"
        )

    text = (
        f"🚛 <b>CROSSDOCK — выбери точку отгрузки</b>\n"
        f"Кластер ({idx + 1}/{len(clusters)}): <b>«{cluster}»</b>\n\n"
        f"Тапни любимую точку или открой полный список хабов / введи имя."
        f"{apply_all_hint}"
    )
    await state.set_state(OzonBook.pick_dropoff)
    await safe_edit_or_answer(msg, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "obdo:apply_all_toggle")
async def cb_obdo_apply_all_toggle(cb: CallbackQuery, state: FSMContext) -> None:
    """Включить режим «одна точка для всех кластеров»."""
    await state.update_data(ob_dropoff_apply_all=True)
    await cb.answer("🔁 Применится ко всем кластерам")
    if cb.message:
        await _ask_dropoff_for_next_cluster(cb.message, state)


@router.callback_query(F.data.startswith("obdo:fav:"))
async def cb_obdo_fav(cb: CallbackQuery, state: FSMContext) -> None:
    """Выбрана любимая точка."""
    try:
        wh_id = int(cb.data.split(":")[2])
    except (ValueError, IndexError):
        await cb.answer("Битый callback", show_alert=True)
        return
    from src.db.models import FavoriteCrossdockPoint
    with db_session() as session:
        f = (
            session.query(FavoriteCrossdockPoint)
            .filter(FavoriteCrossdockPoint.warehouse_id == wh_id)
            .first()
        )
        if not f:
            await cb.answer("Точка пропала", show_alert=True)
            return
        # Бумп счётчик использования
        f.use_count = (f.use_count or 0) + 1
        f.last_used_at = datetime.utcnow()
        name = f.name
    await _accept_dropoff(cb, state, wh_id=wh_id, name=name)


async def _accept_dropoff(
    cb: CallbackQuery, state: FSMContext, *, wh_id: int, name: str,
) -> None:
    """Сохранить выбор для текущего кластера. Если включён режим «одна точка
    на все» — применяет сразу ко всем кластерам и переходит к draft_create."""
    data = await state.get_data()
    clusters: List[str] = data.get("ob_clusters") or []
    idx: int = int(data.get("ob_cluster_idx") or 0)
    choices: Dict = data.get("ob_dropoff_choices") or {}
    apply_all: bool = bool(data.get("ob_dropoff_apply_all"))
    if idx >= len(clusters):
        await cb.answer("Все кластеры уже выбраны", show_alert=True)
        return

    if apply_all:
        for c in clusters:
            choices[c] = {"wh_id": wh_id, "name": name}
        await state.update_data(ob_dropoff_choices=choices, ob_cluster_idx=len(clusters))
        await cb.answer(f"✓ {name[:30]} применено ко всем")
    else:
        cluster = clusters[idx]
        choices[cluster] = {"wh_id": wh_id, "name": name}
        await state.update_data(ob_dropoff_choices=choices, ob_cluster_idx=idx + 1)
        await cb.answer(f"✓ {name[:30]}")
    if cb.message:
        await _ask_dropoff_for_next_cluster(cb.message, state)


@router.callback_query(F.data == "obdo:all")
async def cb_obdo_all(cb: CallbackQuery, state: FSMContext) -> None:
    """Показать все доступные CROSS_DOCK-хабы из локального кэша cluster_list."""
    await cb.answer()
    from src.integrations._cache import cache_get
    clusters = cache_get("ozon_clusters", max_age_sec=86400 * 7) or []
    hubs: List[Dict] = []
    seen_ids = set()
    for cl in clusters:
        cl_name = cl.get("name") or ""
        for lc in (cl.get("logistic_clusters") or []):
            for wh in (lc.get("warehouses") or []):
                wtype = (wh.get("type") or "").upper()
                if wtype != "CROSS_DOCK":
                    continue
                wid = int(wh.get("warehouse_id") or 0)
                if not wid or wid in seen_ids:
                    continue
                seen_ids.add(wid)
                hubs.append({
                    "wh_id": wid,
                    "name": wh.get("name") or f"#{wid}",
                    "type": "CROSS_DOCK",
                    "cluster": cl_name,
                })
    hubs.sort(key=lambda h: h["name"])
    if not hubs:
        if cb.message:
            await cb.message.answer(
                "⚠ Список хабов пуст. Жми «✏ Ввести имя точки» и поищи вручную."
            )
        return
    await state.update_data(obdo_hubs=hubs, obdo_hubs_offset=0)
    if cb.message:
        await _render_obdo_hubs_page(cb.message, state, offset=0)


async def _render_obdo_hubs_page(msg: Message, state: FSMContext, offset: int) -> None:
    """Страница списка всех CROSS_DOCK-хабов с пагинацией."""
    data = await state.get_data()
    hubs: List[Dict] = data.get("obdo_hubs") or []
    total = len(hubs)
    if not hubs:
        await msg.answer("Хабы пропали из кэша.")
        return
    PAGE = 8
    offset = max(0, min(offset, total - 1))
    page = hubs[offset:offset + PAGE]
    rows: List[List[InlineKeyboardButton]] = []
    for h in page:
        label = f"{h['name'][:48]}"[:62]
        rows.append([InlineKeyboardButton(
            text=label, callback_data=f"obdo:hub:{h['wh_id']}",
        )])
    nav: List[InlineKeyboardButton] = []
    if offset > 0:
        nav.append(InlineKeyboardButton(
            text="◀ Назад", callback_data=f"obdo:hubpage:{max(0, offset - PAGE)}",
        ))
    if offset + PAGE < total:
        nav.append(InlineKeyboardButton(
            text="Вперёд ▶", callback_data=f"obdo:hubpage:{offset + PAGE}",
        ))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="◀ К выбору", callback_data="obdo:back")])
    page_n = (offset // PAGE) + 1
    pages_total = (total + PAGE - 1) // PAGE
    await safe_edit_or_answer(
        msg,
        f"🏭 <b>Все CROSS_DOCK-хабы в API</b> (стр. {page_n}/{pages_total}, всего {total})\n"
        "<i>Тапни нужный. ⚠ Не все хабы могут подойти конкретно к твоему "
        "кабинету — Ozon скажет на этапе scoring.</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("obdo:hubpage:"))
async def cb_obdo_hubpage(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    try:
        offset = int(cb.data.split(":")[2])
    except (ValueError, IndexError):
        offset = 0
    if cb.message:
        await _render_obdo_hubs_page(cb.message, state, offset=offset)


@router.callback_query(F.data.startswith("obdo:hub:"))
async def cb_obdo_hub_pick(cb: CallbackQuery, state: FSMContext) -> None:
    """Юзер выбрал хаб из общего списка."""
    try:
        wh_id = int(cb.data.split(":")[2])
    except (ValueError, IndexError):
        await cb.answer("Битый callback", show_alert=True)
        return
    data = await state.get_data()
    hubs: List[Dict] = data.get("obdo_hubs") or []
    selected = next((h for h in hubs if int(h["wh_id"]) == wh_id), None)
    name = (selected or {}).get("name") or f"#{wh_id}"
    await _accept_dropoff(cb, state, wh_id=wh_id, name=name)


@router.callback_query(F.data == "obdo:back")
async def cb_obdo_back(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    if cb.message:
        await _ask_dropoff_for_next_cluster(cb.message, state)


@router.callback_query(F.data == "obdo:input")
async def cb_obdo_input(cb: CallbackQuery, state: FSMContext) -> None:
    """Запросить ввод имени для поиска drop-off."""
    await cb.answer()
    await state.set_state(OzonBook.pick_dropoff_input)
    if cb.message:
        await cb.message.answer(
            "✏ Напиши часть имени точки (минимум 4 символа): например "
            "<code>Хоругвино</code>, <code>Пушкино</code>, <code>Щербинка</code>.\n"
            "Или отправь warehouse_id числом."
        )


@router.message(OzonBook.pick_dropoff_input)
async def msg_obdo_input(msg: Message, state: FSMContext) -> None:
    """Ловим текст → ищем точки через favorites._search_warehouses."""
    query = (msg.text or "").strip()
    if not query:
        await msg.answer("Пустой запрос. Попробуй ещё раз или жми ✖ Отмена.")
        return
    data = await state.get_data()
    tg_id = int(data.get("ob_tg_id") or current_user_id_from(msg) or 0)
    if query.isdigit() and len(query) >= 6:
        wh_id = int(query)
        from src.bot.handlers.favorites import _resolve_warehouse_name
        name = await _resolve_warehouse_name(wh_id, tg_id=tg_id) or f"#{wh_id}"
        # Симулируем callback для _accept_dropoff
        await _accept_dropoff_msg(msg, state, wh_id=wh_id, name=name)
        return

    from src.bot.handlers.favorites import _search_warehouses
    matches = await _search_warehouses(query, tg_id=tg_id)
    if not matches:
        await msg.answer(
            f"❌ Не нашёл точку по «{query}». Попробуй другое имя или ID."
        )
        return

    # Сортируем тем же приоритетом, что и в favorites
    from src.bot.handlers.favorites import _type_priority, _type_label
    matches.sort(key=lambda m: (_type_priority(m.get("type", "")), m["name"]))

    rows: List[List[InlineKeyboardButton]] = []
    for m in matches[:8]:
        label = f"{m['name'][:35]} · {_type_label(m.get('type',''))}"[:62]
        rows.append([InlineKeyboardButton(
            text=label, callback_data=f"obdo:pick:{m['wh_id']}",
        )])
    rows.append([InlineKeyboardButton(text="✖ Отмена", callback_data="obdo:back")])
    # Запомним matches в state
    await state.update_data(obdo_search_matches=matches)
    await msg.answer(
        f"Нашёл по «{query}»: {len(matches)} точек. Выбери нужную:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("obdo:pick:"))
async def cb_obdo_pick(cb: CallbackQuery, state: FSMContext) -> None:
    """Юзер выбрал точку из поиска."""
    try:
        wh_id = int(cb.data.split(":")[2])
    except (ValueError, IndexError):
        await cb.answer("Битый callback", show_alert=True)
        return
    data = await state.get_data()
    matches = data.get("obdo_search_matches") or []
    selected = next((m for m in matches if int(m.get("wh_id", 0)) == wh_id), None)
    name = (selected or {}).get("name") or f"#{wh_id}"
    await _accept_dropoff(cb, state, wh_id=wh_id, name=name)


async def _accept_dropoff_msg(
    msg: Message, state: FSMContext, *, wh_id: int, name: str,
) -> None:
    """Версия _accept_dropoff для Message (после input-flow без CallbackQuery)."""
    data = await state.get_data()
    clusters: List[str] = data.get("ob_clusters") or []
    idx: int = int(data.get("ob_cluster_idx") or 0)
    choices: Dict = data.get("ob_dropoff_choices") or {}
    apply_all: bool = bool(data.get("ob_dropoff_apply_all"))
    if idx >= len(clusters):
        await msg.answer("Все кластеры уже выбраны.")
        return
    if apply_all:
        for c in clusters:
            choices[c] = {"wh_id": wh_id, "name": name}
        await state.update_data(ob_dropoff_choices=choices, ob_cluster_idx=len(clusters))
    else:
        cluster = clusters[idx]
        choices[cluster] = {"wh_id": wh_id, "name": name}
        await state.update_data(ob_dropoff_choices=choices, ob_cluster_idx=idx + 1)
    await _ask_dropoff_for_next_cluster(msg, state)
