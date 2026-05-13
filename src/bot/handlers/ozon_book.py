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

from src.bot.helpers import safe_edit_or_answer, send_long
from src.config import APIKEY_OZON, CLIENT_ID_OZON, OZON_PROXY_URL
from src.db.models import ShipmentRequest, ShipmentItem, Sku
from src.db.session import db_session
from src.integrations import OzonClient, OzonAPIError
from src.services.shipment_service import get_shipment_request
from src.services.slot_hunter import _ozon_cluster_to_name, _normalize

router = Router()
logger = logging.getLogger("bot.ozon_book")


class OzonBook(StatesGroup):
    pick_type = State()
    pick_warehouse = State()
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
    """Собрать items для draft из заявки.
    Возвращает (items, missing_articles).
    items: [{"sku": int, "quantity": int}]  ← Ozon API требует sku (числовой product_id).
    """
    items: List[Dict] = []
    missing: List[str] = []
    by_sku: Dict[int, int] = {}
    for it in req.items:
        if it.marketplace != "ozon" or it.cluster != cluster:
            continue
        sku = it.sku
        if not sku or not sku.ozon_sku:
            missing.append(it.raw_article)
            continue
        by_sku[sku.ozon_sku] = by_sku.get(sku.ozon_sku, 0) + it.qty
    for ozon_sku, qty in by_sku.items():
        items.append({"sku": ozon_sku, "quantity": qty})
    return items, missing


async def _resolve_ozon_cluster_id(oz: OzonClient, cluster_name_local: str) -> Optional[int]:
    """Найти macrolocal_cluster_id у Ozon API по нашему имени кластера.
    С 03.2026 новые endpoint draft/*/create требуют именно macrolocal_cluster_id,
    а не старый cluster.id из cluster_list.
    """
    matched_key = _ozon_cluster_to_name(cluster_name_local)
    if not matched_key:
        return None
    try:
        clusters = await oz.cluster_list()
    except OzonAPIError as e:
        logger.warning("cluster_list failed: %s", e)
        return None
    target_norm = _normalize(matched_key)
    target_norm_local = _normalize(cluster_name_local)
    for cl in clusters:
        cname = cl.get("name") or ""
        n = _normalize(cname)
        if n == target_norm or n == target_norm_local or target_norm in n or n in target_norm:
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
    await _start_ozon_book_wizard(msg, state, rid)


@router.callback_query(F.data.startswith("ozon_book_card:"))
async def cb_ozon_book_from_card(cb: CallbackQuery, state: FSMContext) -> None:
    """Триггер /ozon_book из карточки заявки. Формат: ozon_book_card:<rid> или ozon_book_card:<rid>:<mode>.
    Mode: 'direct' (default) | 'cross'.
    """
    parts = cb.data.split(":")
    rid = int(parts[1])
    mode = parts[2] if len(parts) >= 3 else "direct"
    await cb.answer(f"Запускаю Ozon-мастер ({mode.upper()})…")
    if cb.message:
        await _start_ozon_book_wizard(cb.message, state, rid, mode=mode)


async def _start_ozon_book_wizard(
    msg: Message, state: FSMContext, rid: int, *, mode: str = "direct",
) -> None:
    if not APIKEY_OZON or not CLIENT_ID_OZON:
        await msg.answer("⚠ Ozon-ключи не заданы. /api_check для проверки.")
        return

    # Собираем Ozon-направления заявки
    summaries: List[Tuple[str, int, int, List[str]]] = []  # (cluster, n_items, total_qty, missing)
    with db_session() as session:
        req = get_shipment_request(session, rid)
        if not req:
            await msg.answer(f"Заявка #{rid} не найдена.")
            return
        if not req.target_date_from:
            await msg.answer(
                f"⚠ У заявки #{rid} нет целевых дат. Сначала /ship_plan."
            )
            return

        oz_clusters = sorted({it.cluster for it in req.items if it.marketplace == "ozon"})
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

    if not summaries:
        await msg.answer(f"В заявке #{rid} нет Ozon-направлений.")
        return

    lines = [f"📦 <b>Создание Ozon-поставок для заявки #{rid}</b>\n"]
    has_missing = False
    for cl, n_items, total_qty, missing in summaries:
        lines.append(f"<b>«{cl}»</b>: {n_items} SKU, {total_qty} шт")
        if missing:
            has_missing = True
            lines.append(f"  ⚠ Без offer_id ({len(missing)}): {', '.join(missing[:5])}")
    lines.append(f"\nДаты: {date_from} — {date_to}")

    if has_missing:
        lines.append(
            "\n💡 Запусти /sku_link_ozon чтобы привязать недостающие SKU к Ozon offer_id + sku."
        )

    # mode: "direct" → DIRECT поставка (везти на РФЦ); "cross" → CROSSDOCK (везти в хаб)
    ob_type = "CREATE_TYPE_CROSSDOCK" if mode == "cross" else "CREATE_TYPE_DIRECT"
    type_label = "CROSSDOCK 🚛" if mode == "cross" else "DIRECT 🚀"
    await state.update_data(
        ob_rid=rid,
        ob_clusters=[s[0] for s in summaries],
        ob_date_from=date_from,
        ob_date_to=date_to,
        ob_date_picks=date_picks,
        ob_type=ob_type,
        ob_wh_choices={},
        ob_cluster_idx=0,
    )
    lines.append(f"\n📦 Режим: <b>{type_label}</b>")
    lines.append("\n⏳ Создаю draft → получу scored склады → покажу варианты…")
    await msg.answer("\n".join(lines))
    await _create_drafts_and_fetch_scoring(msg, state)


async def _fetch_scoring_persistent(
    oz: OzonClient, draft_id: int, msg: Message,
) -> List[Dict]:
    """Тянем draft/create/info до scored-результата.

    «Спокойный режим»: 4 попытки × 60-90 сек с jitter ≈ 4-6 мин окно.
    Один запрос за итерацию (без внутренних ретраев) — частые попытки
    при глобальной перегрузке только усугубляют 429 и тратят свои слоты.
    Scoring у Ozon действительно может считаться дольше минуты,
    особенно для CROSSDOCK.

    На пустой scoring НЕ делаем blind-pick: blind wh не в scoring → 404
    на timeslot/info, что усугубляет бан."""
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
                await msg.answer(
                    f"  🚫 Ozon scoring/create-info в anti-abuse cooldown.\n"
                    f"     <code>{err[:300]}</code>\n"
                    f"     Не ретраю — продлит бан."
                )
                return []
            if attempt + 1 < max_outer:
                delay = base_delay + random.randint(0, 30)
                await msg.answer(
                    f"  ⏳ scoring попытка {attempt+1}/{max_outer}: "
                    f"{err[:150]}. Жду {delay}с…"
                )
                await asyncio.sleep(delay)
                continue
            await msg.answer(
                f"  ❌ Scoring не получен за ~{max_outer*(base_delay+15)//60} мин. "
                f"Последняя ошибка: <code>{err[:200]}</code>"
            )
            return []

        clusters_info = info.get("clusters") or []
        status = (clusters_info[0] if clusters_info else {}).get("status") or info.get("status")
        status_upper_top = str(status or "").upper()
        # FAILED + errors[].items_validation — фатальный отказ Ozon (товар
        # не в ассортименте кластера, и т.п.). Ретраить бесполезно — это
        # серверная политика, а не «scoring ещё считается».
        errors = info.get("errors") or []
        if status_upper_top == "FAILED" and errors:
            lines: List[str] = []
            for err in errors[:3]:
                err_msg = err.get("error_message") or err.get("message") or "?"
                validations = err.get("items_validation") or []
                if validations:
                    for v in validations[:5]:
                        for ri in (v.get("rejected_items") or [])[:5]:
                            reasons = ", ".join(ri.get("reasons") or [])
                            lines.append(
                                f"   SKU <code>{ri.get('sku')}</code> в кластере {v.get('macrolocal_cluster_id')}: {reasons}"
                            )
                else:
                    reasons = ", ".join(err.get("error_reasons") or [])
                    lines.append(f"   {err_msg}: {reasons}")
            detail = "\n".join(lines) if lines else "(детали Ozon не вернул)"
            await msg.answer(
                f"  🚫 <b>Ozon отклонил draft</b> (status=FAILED).\n{detail}\n\n"
                f"<i>Самая частая причина OUT_OF_ASSORTMENT — товар не в "
                f"ассортименте кластера для FBO. Иногда Ozon ЛК пускает в обход API "
                f"(другой контракт/тип поставки). Проверь карточку товара в Seller "
                f"Center: «Доступность по кластерам» / «Регионы».</i>"
            )
            return []
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
                state = str(st.get("state") or "").upper()
                invalid_reason = str(st.get("invalid_reason") or "").upper()
                is_available = st.get("is_available")
                # v2 availability_status.state enum: FULL_AVAILABLE / PARTIAL_AVAILABLE
                # (склад доступен), INVALID (нельзя), UNSPECIFIED (ещё считается).
                # invalid_reason="UNSPECIFIED" значит "нет причины невалидности" —
                # т.е. склад валидный, это НЕ pending.
                available_states = {"FULL_AVAILABLE", "PARTIAL_AVAILABLE", "AVAILABLE", "SUCCESS"}
                if is_available is None:
                    is_available = state in available_states
                pending = state == "UNSPECIFIED"
                if pending:
                    n_unspecified += 1
                wh_list.append({
                    "wh_id": int(wh_id),
                    "name": name,
                    "score": w.get("total_score", 0),
                    "rank": w.get("total_rank", 0),
                    "available": bool(is_available),
                    "pending": pending,
                    "reason": invalid_reason if invalid_reason and invalid_reason != "UNSPECIFIED" else state,
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
        status_upper = str(status or "").upper()
        scoring_in_progress = (
            not wh_list
            or status_upper in {"CALCULATION_STATUS_IN_PROGRESS", "IN_PROGRESS"}
            or (n_avail == 0 and n_unspecified > 0)
        )
        if scoring_in_progress:
            if attempt + 1 < max_outer:
                delay = base_delay + random.randint(0, 30)
                hint = (
                    f"UNSPECIFIED={n_unspecified}, дозревает"
                    if n_unspecified > 0 else "IN_PROGRESS"
                )
                await msg.answer(
                    f"  ⏳ Scoring дозревает ({hint}, попытка {attempt+1}/{max_outer}). Жду {delay}с…"
                )
                await asyncio.sleep(delay)
                continue
            await msg.answer(f"  ❌ Scoring так и не посчитался за ~{max_outer*(base_delay+15)//60} мин.")
            return []

        await msg.answer(f"  ✅ Scored получили: {len(wh_list)} складов, доступно {n_avail}")
        return wh_list
    return wh_list


async def _create_drafts_and_fetch_scoring(msg: Message, state: FSMContext) -> None:
    """Создать draft для каждого кластера + получить scored склады через draft/create/info.
    Сохраняет drafts + scored_warehouses в state, потом показывает picker."""
    data = await state.get_data()
    rid = data["ob_rid"]
    clusters = data["ob_clusters"]
    draft_type = data["ob_type"]
    # Ozon enum: 2=DIRECT (точно), CROSSDOCK = 1 (Ozon принимает но раньше отвергал
    # storage_warehouse_id, теперь шлём drop_off_warehouse_id)
    supply_type = 1 if "CROSSDOCK" in (draft_type or "").upper() else 2

    oz = OzonClient(CLIENT_ID_OZON, APIKEY_OZON, proxy=OZON_PROXY_URL)
    drafts_made: List[Dict] = []
    scored_by_cluster: Dict[str, List[Dict]] = {}  # cluster → [{wh_id, name, score, available, reason}]

    # Pre-check: проверяем, что все ozon_sku из заявки реально есть в текущем
    # Ozon-кабинете. Без этого мы рискуем получить OUT_OF_ASSORTMENT (если
    # ozon_sku из другого кабинета) или, что хуже, успешно отправить мусор.
    all_skus_to_check: List[int] = []
    with db_session() as session:
        req = get_shipment_request(session, rid)
        if req:
            for cl in clusters:
                items_check, _ = _build_items_for_cluster(req, cl)
                for it in items_check:
                    if it["sku"] not in all_skus_to_check:
                        all_skus_to_check.append(it["sku"])
    if all_skus_to_check:
        await msg.answer("🔍 Сверяю SKU с актуальным Ozon-кабинетом…")
        missing, valid_map = await _validate_skus_in_current_account(oz, all_skus_to_check)
        if missing:
            # Подтащим article+offer_id для понятного сообщения
            lines: List[str] = []
            with db_session() as session:
                from src.db.models import Sku
                bad_skus = session.query(Sku).filter(Sku.ozon_sku.in_(missing)).all()
                seen_sku = set()
                for s in bad_skus:
                    seen_sku.add(s.ozon_sku)
                    lines.append(
                        f"  • <code>{s.article}</code> (offer_id=<code>{s.ozon_offer_id or '?'}</code>, "
                        f"sku=<code>{s.ozon_sku}</code>)"
                    )
                for s in missing:
                    if s not in seen_sku:
                        lines.append(f"  • sku=<code>{s}</code> (нет в нашей БД)")
            await msg.answer(
                f"🚫 <b>Стоп — артикулы не из текущего кабинета.</b>\n\n"
                f"В Ozon (client_id={CLIENT_ID_OZON}) нет таких SKU:\n"
                + "\n".join(lines[:15])
                + ("\n  …" if len(lines) > 15 else "")
                + "\n\nОзон ответил бы <code>OUT_OF_ASSORTMENT</code> и заявка бы не прошла.\n"
                "<i>Открой меню → 🔗 Привязать каталог → или </i><code>/sku_link_ozon</code><i> "
                "чтобы пересинхронизировать SKU.</i>"
            )
            await state.clear()
            return

    for cl in clusters:
        await msg.answer(f"🔄 Кластер <b>«{cl}»</b>…")
        try:
            cid = await _resolve_ozon_cluster_id(oz, cl)
        except OzonAPIError as e:
            await msg.answer(f"⚠ cluster_list: <code>{str(e)[:200]}</code>")
            continue
        if not cid:
            await msg.answer(f"⚠ Не сматчил «{cl}» с Ozon-кластером. Пропускаю.")
            continue

        with db_session() as session:
            req = get_shipment_request(session, rid)
            if not req:
                await msg.answer(f"Заявка #{rid} пропала.")
                return
            items, _ = _build_items_for_cluster(req, cl)
        if not items:
            await msg.answer(f"⚠ «{cl}»: нет SKU с offer_id — нечего бронировать.")
            continue

        endpoint_label = "/v1/draft/crossdock/create" if supply_type == 1 else "/v1/draft/direct/create"
        await msg.answer(
            f"  POST {endpoint_label}: cluster_id={cid}, items={len(items)} (жду 15 сек)"
        )
        await asyncio.sleep(15.0)
        try:
            op_id = await oz.draft_create(
                items=items, cluster_ids=[cid], draft_type=draft_type,
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

        drafts_made.append({
            "cluster": cl, "cluster_id": cid, "draft_id": draft_id,
            "supply_type": supply_type,
        })
        await msg.answer(
            f"  ✅ draft_id=<code>{draft_id}</code>, тяну scored склады "
            f"(до 5 мин на ретраи если Ozon занят)…"
        )

        # Получаем scored. До 3 минут ретраев.
        # Если scoring пуст — НЕ делаем blind-pick (это создавало 404
        # на timeslot/info и продлевало anti-abuse бан).
        wh_list = await _fetch_scoring_persistent(oz, draft_id, msg)
        scored_by_cluster[cl] = wh_list

    if not drafts_made:
        await msg.answer("⚠ Ни один draft не создан.")
        await state.clear()
        return

    await state.update_data(
        ob_drafts=drafts_made,
        ob_scored=scored_by_cluster,
        ob_date_from_iso=f"{data['ob_date_from']}T00:00:00Z",
        ob_date_to_iso=f"{data['ob_date_to']}T23:59:59Z",
    )
    await _show_scored_warehouse_picker(msg, state)


async def _show_scored_warehouse_picker(msg: Message, state: FSMContext) -> None:
    """Показать кнопки scored складов для текущего кластера."""
    data = await state.get_data()
    clusters = data["ob_clusters"]
    idx = data.get("ob_cluster_idx", 0)
    if idx >= len(clusters):
        await msg.answer("✅ Все кластеры выбраны.")
        await state.clear()
        return

    cluster = clusters[idx]
    scored = (data.get("ob_scored") or {}).get(cluster) or []
    available = [w for w in scored if w["available"]]
    unavailable = [w for w in scored if not w["available"]]

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
    if unavailable:
        rows.append([InlineKeyboardButton(
            text=f"ℹ Недоступно: {len(unavailable)} (скрыто)",
            callback_data=f"obscored_noop:{idx}",
        )])
    rows.append([InlineKeyboardButton(text="◀ К карточке заявки",
                                      callback_data=f"ship_open:{data['ob_rid']}")])

    progress = f"({idx + 1}/{len(clusters)})" if len(clusters) > 1 else ""
    await state.set_state(OzonBook.pick_warehouse)
    await msg.answer(
        f"📍 <b>«{cluster}» {progress}</b> — {len(available)} складов\n\n"
        f"«🚀 Auto-walk» — бот сам пойдёт по списку, остановится на первом со слотами.\n"
        f"«🥇🥈🥉🎯» — выбрать конкретный (мб 404 «not in scoring» или 0 слотов).",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("obautowalk:"))
async def cb_ob_autowalk(cb: CallbackQuery, state: FSMContext) -> None:
    """Бот сам пробует склады из списка пока не получит 200 со слотами."""
    idx = int(cb.data.split(":", 1)[1])
    data = await state.get_data()
    cluster = data["ob_clusters"][idx]
    scored = (data.get("ob_scored") or {}).get(cluster) or []
    available = [w for w in scored if w["available"]]

    await cb.answer("Запускаю auto-walk…")
    if not cb.message:
        return

    if not available:
        await cb.message.answer("⚠ Нет складов для перебора.")
        return

    # Готовим draft для текущего кластера
    drafts = data.get("ob_drafts") or []
    draft = next((d for d in drafts if d["cluster"] == cluster), None)
    if not draft:
        await cb.message.answer("⚠ Draft не найден.")
        return

    date_from_iso = data["ob_date_from_iso"]
    date_to_iso = data["ob_date_to_iso"]
    oz = OzonClient(CLIENT_ID_OZON, APIKEY_OZON, proxy=OZON_PROXY_URL)

    found_slots: List[Dict] = []
    tried = 0
    summary: List[str] = []  # отчёт по каждому wh для финального лога
    status_msg = await cb.message.answer(
        f"🔄 Auto-walk: пробую {len(available)} складов.\n"
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
        slots: List[Dict] = []
        for e in entries:
            slots.append({
                "draft_id": draft["draft_id"],
                "cluster": cluster,
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
    await _post_found_slots(cb.message.bot, cb.message.chat.id, data["ob_rid"], found_slots)


@router.callback_query(F.data.startswith("obscored_noop:"))
async def cb_ob_scored_noop(cb: CallbackQuery) -> None:
    await cb.answer("Эти склады недоступны (scoring)", show_alert=True)


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

    oz = OzonClient(CLIENT_ID_OZON, APIKEY_OZON, proxy=OZON_PROXY_URL)

    # Pre-check SKU (см. _create_drafts_and_fetch_scoring — та же логика)
    all_skus_to_check: List[int] = []
    with db_session() as session:
        req = get_shipment_request(session, rid)
        if req:
            for cl in clusters:
                items_check, _ = _build_items_for_cluster(req, cl)
                for it in items_check:
                    if it["sku"] not in all_skus_to_check:
                        all_skus_to_check.append(it["sku"])
    if all_skus_to_check:
        await msg.answer("🔍 Сверяю SKU с актуальным Ozon-кабинетом…")
        missing, _ = await _validate_skus_in_current_account(oz, all_skus_to_check)
        if missing:
            lines: List[str] = []
            with db_session() as session:
                from src.db.models import Sku
                bad_skus = session.query(Sku).filter(Sku.ozon_sku.in_(missing)).all()
                seen_sku = set()
                for s in bad_skus:
                    seen_sku.add(s.ozon_sku)
                    lines.append(
                        f"  • <code>{s.article}</code> (offer_id=<code>{s.ozon_offer_id or '?'}</code>, "
                        f"sku=<code>{s.ozon_sku}</code>)"
                    )
                for s in missing:
                    if s not in seen_sku:
                        lines.append(f"  • sku=<code>{s}</code> (нет в нашей БД)")
            await msg.answer(
                f"🚫 <b>Стоп — артикулы не из текущего кабинета.</b>\n\n"
                f"В Ozon (client_id={CLIENT_ID_OZON}) нет таких SKU:\n"
                + "\n".join(lines[:15])
                + ("\n  …" if len(lines) > 15 else "")
                + "\n\nОзон ответил бы <code>OUT_OF_ASSORTMENT</code> и заявка бы не прошла.\n"
                "<i>Открой меню → 🔗 Привязать каталог → или </i><code>/sku_link_ozon</code><i> "
                "чтобы пересинхронизировать SKU.</i>"
            )
            await state.clear()
            return

    # Под каждый кластер — отдельный draft с выбранным drop-off складом
    drafts_made: List[Dict] = []
    for cl in clusters:
        wh_id = wh_choices.get(cl)  # None = «любой» (не передаём в draft)
        wh_label = ""
        if wh_id:
            wh_list = _get_cluster_ff_warehouses(cl)
            wh_name = next((w["name"] for w in wh_list if w["wh_id"] == wh_id), f"#{wh_id}")
            wh_label = f" → {wh_name}"
        await msg.answer(f"🔄 Кластер <b>«{cl}»</b>{wh_label}…")

        # Резолвим cluster_id
        try:
            cid = await _resolve_ozon_cluster_id(oz, cl)
        except OzonAPIError as e:
            await msg.answer(f"⚠ cluster_list: <code>{str(e)[:200]}</code>")
            continue
        if not cid:
            await msg.answer(f"⚠ Не сматчил «{cl}» с Ozon-кластером. Пропускаю.")
            continue

        # Собираем items
        with db_session() as session:
            req = get_shipment_request(session, rid)
            if not req:
                await msg.answer(f"Заявка #{rid} пропала.")
                return
            items, missing = _build_items_for_cluster(req, cl)

        if not items:
            await msg.answer(f"⚠ «{cl}»: нет SKU с offer_id — нечего бронировать.")
            continue

        # Для DIRECT: НЕ передаём wh в draft (это поле — drop_off_point, для CROSSDOCK).
        # WH будет фильтром в timeslot/info через selected_cluster_warehouses.storage_warehouse_ids
        wh_log = f", target_wh={wh_id} (для timeslot, не draft)" if wh_id else ""
        await msg.answer(
            f"  POST /v1/draft/direct/create: cluster_id={cid}, items={len(items)}{wh_log}… "
            "(жду 15 сек, лимит 2/min)"
        )
        await asyncio.sleep(15.0)
        try:
            op_id = await oz.draft_create(
                items=items,
                cluster_ids=[cid],
                draft_type=draft_type,
                # Не передаём drop_off для DIRECT — Ozon ругается несовместимостью с storage_warehouse_ids
            )
        except OzonAPIError as e:
            await msg.answer(f"❌ draft_create: <code>{str(e)[:400]}</code>")
            continue

        if op_id.startswith("sync:"):
            draft_id = op_id.split(":", 1)[1]
        else:
            await msg.answer(f"  ⏳ operation_id={op_id[:24]}…, polling…")
            info = await _wait_draft_ready(oz, op_id)
            status = info.get("status", "?")
            if "SUCCESS" not in status.upper() and "DONE" not in status.upper():
                errs = info.get("errors") or []
                err_s = "; ".join(str(e)[:120] for e in errs[:3]) if errs else "?"
                await msg.answer(
                    f"❌ draft не готов: status={status}\nerrors: <code>{err_s}</code>"
                )
                continue
            draft_id = info.get("draft_id") or info.get("calculation_id")
        if not draft_id:
            await msg.answer("⚠ Нет draft_id в ответе.")
            continue

        supply_type_int = 1 if "CROSSDOCK" in (draft_type or "").upper() else 2
        drafts_made.append({
            "cluster": cl,
            "cluster_id": cid,
            "draft_id": int(draft_id),
            "operation_id": op_id,
            "items_count": len(items),
            "wh_id": wh_id,  # для фильтра в timeslot/info
            "supply_type": supply_type_int,
        })
        await msg.answer(f"  ✅ draft_id=<code>{draft_id}</code>")

    if not drafts_made:
        await msg.answer("⚠ Ни один draft не создан.")
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

    oz = OzonClient(CLIENT_ID_OZON, APIKEY_OZON, proxy=OZON_PROXY_URL)
    all_buttons: List[List[InlineKeyboardButton]] = []
    slot_counter = 0
    failed_drafts: List[Dict] = []  # для возможного retry

    for d in drafts_made:
        wh_id_filter = d.get("wh_id")
        wh_suffix = f" / wh={wh_id_filter}" if wh_id_filter else ""
        await msg.answer(
            f"📅 Таймслоты для draft #{d['draft_id']} ({d['cluster']}){wh_suffix}…\n"
            "<i>(до 20 сек, если упрётся в глобальный лимит — будет кнопка 🔁)</i>"
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
            await msg.answer(f"⚠ timeslot/info для {d['cluster']}: <code>{str(e)[:600]}</code>")
            failed_drafts.append(d)
            continue

        # Парсим ответ v2: данные под "result.drop_off_warehouse_timeslots"
        # v2 структура (1 склад через selected_cluster_warehouses):
        #   {result: {drop_off_warehouse_timeslots: {days: [{date, timeslots: [...]}]}}}
        # v1 (legacy): массив warehouse-объектов на верхнем уровне.
        parsed_slots = _parse_v2_timeslots(ts, fallback_wh_id=wh_id_filter, fallback_wh_name="")
        if not parsed_slots:
            import json as _json
            raw_dump = _json.dumps(ts, ensure_ascii=False)[:1500]
            await msg.answer(
                f"🔴 Пустой ответ от timeslot/info для «{d['cluster']}».\n"
                f"<b>Raw:</b>\n<code>{raw_dump[:800]}</code>"
            )
            continue

        # Фильтруем по выбранным пользователем датам (Ozon-API принимает только
        # диапазон date_from..date_to и возвращает все дни между ними).
        date_picks = data.get("ob_date_picks") or []
        if date_picks:
            picks_set = set(date_picks)
            total_before = len(parsed_slots)
            parsed_slots = [e for e in parsed_slots if e["from"][:10] in picks_set]
            if total_before and not parsed_slots:
                await msg.answer(
                    f"🔴 Для «{d['cluster']}» слоты есть, но не на выбранные тобой даты "
                    f"({', '.join(sorted(picks_set))}). Открой «🛠 Изменить даты»."
                )
                continue

        lines = [f"🟢 <b>{d['cluster']}</b> — {len(parsed_slots)} слотов"]
        for entry in parsed_slots[:20]:
            slot_counter += 1
            date_short = entry["from"][:10]
            t_hm = entry["from"][11:16]
            btn_label = f"📌 {date_short} {t_hm}"
            cb_data = f"obslot:{slot_counter}"
            all_buttons.append([InlineKeyboardButton(text=btn_label[:40], callback_data=cb_data)])
            await state.update_data(**{
                f"slot_{slot_counter}": {
                    "draft_id": d["draft_id"],
                    "cluster_id": d.get("cluster_id"),
                    "supply_type": d.get("supply_type", 2),
                    "warehouse_id": entry["warehouse_id"],
                    "warehouse_name": entry["warehouse_name"],
                    "from": entry["from"],
                    "to": entry["to"],
                    "cluster": d["cluster"],
                }
            })
            lines.append(f"  {date_short} {entry['from'][11:16]}–{entry['to'][11:16]}")
        await send_long(msg, "\n".join(lines))

    if all_buttons:
        all_buttons.append([InlineKeyboardButton(text="✖ Отмена", callback_data="cancel")])
        await state.set_state(OzonBook.pick_slot)
        await msg.answer(
            f"✅ Найдено {slot_counter} слотов. Выбери:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=all_buttons[:30]),
        )
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
        # Универсальный путь: предлагаем "🔁 Повторить" + "◀ Другой склад"
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


async def _auto_poll_slots(
    bot,
    chat_id: int,
    rid: int,
    drafts: List[Dict],
    date_from_iso: str,
    date_to_iso: str,
    date_picks: Optional[List[str]] = None,
) -> None:
    """Раз в 60 сек дёргает timeslot/info. До 25 мин (draft живёт 30 мин).
    При успехе — постит слоты пользователю и завершается.
    При реальном «слотов нет» — тоже завершается с уведомлением.
    """
    import time as _t

    deadline = _t.time() + 25 * 60  # 25 минут
    interval = 60                    # секунд между попытками
    attempts = 0
    status_msg_id: Optional[int] = None

    logger.info("auto-poll started: rid=%d, drafts=%d", rid, len(drafts))
    try:
        # Первая попытка через 60 сек (initial-окно уже отработало в _fetch_slots_for_drafts)
        await asyncio.sleep(interval)

        while _t.time() < deadline:
            attempts += 1
            oz = OzonClient(CLIENT_ID_OZON, APIKEY_OZON, proxy=OZON_PROXY_URL)
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
                    # 404 «scoring result not found» = draft/wh связка сломана.
                    # Ретраить бесполезно (новых scored wh не появится) + каждый
                    # хит может продлевать anti-abuse. Гасим auto-poll.
                    if "404" in err_s and "scoring" in err_s.lower():
                        await bot.send_message(
                            chat_id,
                            f"🛑 Авто-поиск #{rid} остановлен: 404 «scoring not found». "
                            f"Связка draft+склад невалидна — нужно создать draft заново "
                            f"(scoring должен успеть посчитаться, не делайте blind-pick склада)."
                        )
                        logger.info("auto-poll stopped on 404 scoring: rid=%d", rid)
                        return
                    logger.warning("auto-poll API error: %s", e)
                except Exception as e:
                    logger.exception("auto-poll unexpected: %s", e)

            if all_slot_entries:
                # Успех — постим слоты
                await _post_found_slots(bot, chat_id, rid, all_slot_entries)
                logger.info("auto-poll success: rid=%d, slots=%d", rid, len(all_slot_entries))
                return

            if any_ok and not all_slot_entries:
                # API ответил, но реально пусто на эти даты
                await bot.send_message(
                    chat_id,
                    f"🔴 Авто-поиск слотов #{rid}: запросы проходят, но <b>на выбранные даты слотов нет</b>. "
                    f"Можешь расширить даты через «🛠 Изменить даты» в карточке.",
                )
                return

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

        # Дедлайн — поставка не получилась
        await bot.send_message(
            chat_id,
            f"⏰ Авто-поиск слотов #{rid} истёк через 25 мин. Лимит Ozon так и не отпустил. "
            f"Drafts протухнут через несколько минут — придётся пересоздавать заново.",
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


async def _post_found_slots(bot, chat_id: int, rid: int, slots: List[Dict]) -> None:
    """Постит найденные слоты пользователю с inline-кнопками."""
    # Складываем слоты в module-cache, чтобы callback мог их достать без FSM
    buttons: List[List[InlineKeyboardButton]] = []
    for i, slot in enumerate(slots[:25]):  # ограничение Telegram на кол-во кнопок
        token = f"{rid}_{i}"
        _FOUND_SLOTS[token] = slot
        date_short = (slot.get("from") or "")[:10]
        t_from = (slot.get("from") or "")[11:16]
        wh_short = (slot.get("warehouse_name") or "")[:14]
        btn_text = f"📌 {date_short} {t_from} {wh_short}"
        buttons.append([InlineKeyboardButton(text=btn_text[:40], callback_data=f"obfslot:{token}")])

    buttons.append([InlineKeyboardButton(text="◀ К карточке заявки", callback_data=f"ship_open:{rid}")])

    await bot.send_message(
        chat_id,
        f"🎉 <b>Слоты найдены!</b> Заявка #{rid} — {len(slots)} вариантов.\n"
        "Тапни нужный — бот забронирует его в Ozon ЛК.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data.startswith("obfslot:"))
async def cb_ob_found_slot_pick(cb: CallbackQuery) -> None:
    """Пользователь тапнул слот из auto-poll результата."""
    token = cb.data.split(":", 1)[1]
    slot = _FOUND_SLOTS.get(token)
    if not slot:
        await cb.answer("Слот пропал из кэша. Запусти Ozon-мастер заново.", show_alert=True)
        return

    # Single-flight: используем draft_id как ключ (уникальный per draft)
    lock_key = f"draft_{slot['draft_id']}"
    if lock_key in _BOOKING_IN_FLIGHT:
        await cb.answer("Бронирование этого слота уже идёт — подожди ответ.", show_alert=True)
        return
    _BOOKING_IN_FLIGHT.add(lock_key)
    try:
        await _do_book_slot(cb, slot)
    finally:
        _BOOKING_IN_FLIGHT.discard(lock_key)


async def _do_book_slot(cb: CallbackQuery, slot: Dict, rid: Optional[int] = None) -> None:
    """Полный flow бронирования через v2: supply/create → polling status → запись в БД.
    Идемпотентно через single-flight в callers.

    С 16.03.2026 финализация переехала на /v2/draft/supply/create + /v2/draft/supply/create/status.
    Старый /v1/draft/supply/create отдаёт code:8 "rate limit per second" даже на свежем
    аккаунте/IP — там новые серверные лимиты, либо он совсем отключён, документация
    прямо помечает его как deprecated.
    """
    await cb.answer("Бронирую…")
    oz = OzonClient(CLIENT_ID_OZON, APIKEY_OZON, proxy=OZON_PROXY_URL)
    if cb.message:
        await safe_edit_or_answer(
            cb.message,
            f"⏳ POST /v2/draft/supply/create\n"
            f"draft_id={slot['draft_id']}\n"
            f"cluster_id={slot.get('cluster_id')}\n"
            f"warehouse={slot['warehouse_name']} (id={slot['warehouse_id']})\n"
            f"timeslot={slot['from'][:16]} — {slot['to'][:16]}"
        )
    cluster_id = slot.get("cluster_id")
    if not cluster_id:
        if cb.message:
            await cb.message.answer(
                "❌ В слоте нет cluster_id (старый кэш). Нажми «🔁 Повторить» в карточке."
            )
        return
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
        if cb.message:
            await cb.message.answer(f"❌ {str(e)[:400]}")
        return

    if errors:
        if cb.message:
            await cb.message.answer(
                f"❌ Ozon отклонил поставку: <code>{', '.join(errors[:5])}</code>"
            )
        return

    if cb.message:
        await cb.message.answer("⏳ supply создаётся, polling /v2/draft/supply/create/status…")

    final = None
    for _ in range(30):
        await asyncio.sleep(2)
        try:
            info = await oz.draft_supply_create_status_v2(slot["draft_id"])
        except OzonAPIError as e:
            if cb.message:
                await cb.message.answer(f"⚠ status: <code>{str(e)[:200]}</code>")
            return
        status = str(info.get("status") or "").upper()
        if status in {"SUCCESS", "FAILED"}:
            final = info
            break

    if not final:
        if cb.message:
            await cb.message.answer("⚠ Таймаут на финализации supply (но в ЛК может появиться).")
        return

    status = str(final.get("status") or "?")
    success = status.upper() == "SUCCESS"
    order_id = final.get("order_id")

    if cb.message:
        if success:
            await cb.message.answer(
                f"✅ <b>Поставка создана в Ozon ЛК!</b>\n"
                f"order_id: <code>{order_id}</code>\n"
                f"Кластер: {slot['cluster']}\n"
                f"Drop-off: {slot['warehouse_name']}\n"
                f"Слот: {slot['from'][:16]} — {slot['to'][:16]}\n\n"
                f"Проверь в Ozon ЛК → FBO → Поставки."
            )
            if rid:
                with db_session() as session:
                    req = get_shipment_request(session, rid)
                    if req:
                        if order_id:
                            for it in req.items:
                                if it.marketplace == "ozon" and it.cluster == slot["cluster"]:
                                    it.booked_supply_id = str(order_id)
                                    it.target_warehouse = slot["warehouse_name"]
                                    it.booked_slot_at = datetime.fromisoformat(
                                        slot["from"].replace("Z", "+00:00").split("+")[0]
                                    )
                        req.state = "supplies_created"
        else:
            errs = final.get("error_reasons") or []
            err_s = ", ".join(str(e) for e in errs[:5])
            await cb.message.answer(f"❌ status={status}\nerrors: <code>{err_s}</code>")


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
            await cb.message.answer(f"✖ Авто-поиск для #{rid} остановлен.")
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


@router.callback_query(OzonBook.pick_slot, F.data.startswith("obslot:"))
async def cb_ob_slot_pick(cb: CallbackQuery, state: FSMContext) -> None:
    slot_n = int(cb.data.split(":", 1)[1])
    data = await state.get_data()
    slot = data.get(f"slot_{slot_n}")
    if not slot:
        await cb.answer("Слот пропал — повтори /ozon_book", show_alert=True)
        return

    rid = data["ob_rid"]
    # Single-flight: блокируем повторный тап
    lock_key = f"draft_{slot['draft_id']}"
    if lock_key in _BOOKING_IN_FLIGHT:
        await cb.answer("Бронирование этого слота уже идёт — подожди.", show_alert=True)
        return
    _BOOKING_IN_FLIGHT.add(lock_key)
    await state.clear()
    try:
        await _do_book_slot(cb, slot, rid=rid)
    finally:
        _BOOKING_IN_FLIGHT.discard(lock_key)
