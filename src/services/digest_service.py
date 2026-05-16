"""Утренняя сводка для владельца кабинета.

Собирает live из Ozon API без снапшотов:
  • возвраты (всего, к получению, на ПВЗ, PDF этикетки)
  • срочно отгрузить (FBO остаток < 7-дневной потребности по 7d-rate)
  • runout (когда товар закончится по 28d-rate; <15д / 15-30д / >30д)

Используется как:
  - утренняя пуш-рассылка через scheduler в bot.main
  - команда /digest для ручного дёрга

Дефолты порогов и top-N подобраны под one-eye-glance:
  если дофига SKU, показываем только хвост рейтинга.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from src.integrations.ozon_api import OzonAPIError, OzonClient

logger = logging.getLogger("services.digest")


# ── Настройки порогов ─────────────────────────────────────────────────────

URGENT_RED_DAYS = 7        # < 7 дней по 7d-rate → 🔴 срочно
URGENT_YELLOW_DAYS = 14    # 7-14 дней по 7d-rate → 🟡 пора подумать
RUNOUT_RED_DAYS = 15       # <15 дней по 28d-rate → 🔴 кончается
RUNOUT_GREEN_DAYS = 30     # >30 дней по 28d-rate → 🟢 в порядке
TOP_URGENT_LIMIT = 5       # топ-5 «срочно отгрузить» — чтобы на 100+ SKU не было каши
TOP_RUNOUT_RED_LIMIT = 5   # топ-5 🔴 «кончится за <15 дней»
TOP_RUNOUT_YELLOW_LIMIT = 3 # топ-3 🟡 «15–30 дней»
TARGET_COVERAGE_DAYS = 56  # «дозаправляем» до 56 дней покрытия (≈ 2 месяца) —
                            # одинаково для urgent (по 7d-rate) и runout (по 28d-rate)


# ── Структуры результата ─────────────────────────────────────────────────

@dataclass
class RemovalGroup:
    """Группа товаров вывоза, сгруппированных по destination_warehouse + статус."""
    warehouse_name: str
    warehouse_address: str
    state: str               # «На пути в ПВЗ» / «В ПВЗ» / «Утилизирован» / etc
    is_at_pvz: bool          # True если в пункте выдачи (given_out_date пустой, delivery_date < now)
    box_count: int           # уникальные box_id
    items_count: int         # суммарное quantity_for_return
    delivery_date: str       # ISO дата прибытия в ПВЗ (если есть)
    sample_offer_ids: List[str]  # пара первых артикулов для inline-просмотра


@dataclass
class ReturnsSummary:
    total: int = 0
    actionable_at_pvz: int = 0     # возвраты, лежат в ПВЗ — забрать
    giveouts_available: int = 0    # партии FBO к вывозу со склада Ozon
    giveouts_at_pvz: int = 0       # партии уже в пункте выдачи продавца
    pdf_bytes: Optional[bytes] = None  # этикетка получения, если есть
    # Removal — вывозы товара продавцу (раздельно: со стока FBO и с поставки)
    removal_from_stock: List[RemovalGroup] = field(default_factory=list)
    removal_from_supply: List[RemovalGroup] = field(default_factory=list)


@dataclass
class SkuLine:
    """Одна строка списка сводки."""
    sku: int
    offer_id: str
    name: str
    stock: int
    rate_per_day: float    # средняя скорость продаж
    days_left: float       # сколько дней до нуля
    color: str             # '🔴' / '🟡' / '🟢'
    to_ship_qty: int = 0   # рекомендованное кол-во к отгрузке (urgent-режим)


@dataclass
class ActAwaitingItem:
    """Поставка FBO в статусе REPORTS_CONFIRMATION_AWAITING / REPORT_REJECTED —
    юзер должен зайти в ЛК и подтвердить акт приёмки."""
    order_id: int
    order_number: str
    state: str               # REPORTS_CONFIRMATION_AWAITING / REPORT_REJECTED
    dropoff_name: str
    state_updated_at: str    # ISO


@dataclass
class DigestData:
    generated_at: datetime
    returns: ReturnsSummary = field(default_factory=ReturnsSummary)
    urgent: List[SkuLine] = field(default_factory=list)   # отсортирован по days_left ↑
    runout: List[SkuLine] = field(default_factory=list)
    acts_awaiting: List[ActAwaitingItem] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)        # в чём недотащили API


# ── Сборщики ──────────────────────────────────────────────────────────────

async def collect_returns_summary(oz: OzonClient) -> Tuple[ReturnsSummary, List[str]]:
    """Сборка блока возвратов. Возвращает (summary, errors).

    `total` = только actionable (на ПВЗ ждут получения). Раньше показывал
    `len(all_returns)` куда попадали Disposed / Received / OnOzonWarehouse —
    это раздувало цифру до сотен «возвратов», хотя реально к получению были
    единицы.
    """
    summary = ReturnsSummary()
    errors: List[str] = []

    try:
        all_returns = await oz.returns_list(limit=500)

        def _bucket(r: dict) -> str:
            v = (r.get("visual") or {}).get("status") or {}
            sys_name = (v.get("sys_name") or "").lower()
            display = (v.get("display_name") or "").lower()
            if "arrivedatreturnplace" in sys_name or "пункт" in display:
                return "actionable"
            return "other"

        summary.actionable_at_pvz = sum(1 for r in all_returns if _bucket(r) == "actionable")
        summary.total = summary.actionable_at_pvz  # «всего активных» = что реально требует внимания
    except OzonAPIError as e:
        logger.warning("returns_list failed: %s", e)
        errors.append(f"returns_list: {str(e)[:200]}")

    try:
        giveouts = await oz.returns_giveout_list(limit=200)
        for g in giveouts:
            st = str(g.get("giveout_status") or "").upper()
            if st in ("CREATED", "APPROVED"):
                summary.giveouts_available += 1
            elif st == "COMPLETED":
                summary.giveouts_at_pvz += 1
    except OzonAPIError as e:
        logger.info("returns_giveout_list failed (likely not enabled): %s", e)
        # Не пишем в errors — ничего страшного, у некоторых продавцов выключен.

    # PDF этикетки тянем всегда. Ozon отдаёт «универсальную этикетку получения
    # партии» — актуальна когда есть giveouts/removals в принципе. Раньше
    # фильтровал «только если есть giveouts», но юзер хотел чтобы PDF приходил
    # сразу с дайджестом без ручного запроса. Если PDF реально пустой — len<500,
    # тогда не сохраняем.
    try:
        pdf = await oz.returns_giveout_get_pdf()
        if pdf and len(pdf) > 500:
            summary.pdf_bytes = pdf
    except OzonAPIError as e:
        logger.info("returns_giveout_get_pdf failed: %s", e)

    # Вывозы товара продавцу — со стока FBO и с поставки (за 60 дней).
    # Группируем по destination_warehouse + return_state, чтобы юзер видел
    # «3 коробки в ПВЗ Кантемировская» а не 30 одинаковых строк.
    now = datetime.now(timezone.utc)
    rem_from = (now - timedelta(days=60)).strftime("%Y-%m-%d")
    rem_to = now.strftime("%Y-%m-%d")
    try:
        rows = await oz.removal_from_stock_list(rem_from, rem_to, max_total=2000)
        summary.removal_from_stock = _group_removals(rows)
    except OzonAPIError as e:
        logger.info("removal_from_stock_list failed: %s", e)
    try:
        rows = await oz.removal_from_supply_list(rem_from, rem_to, max_total=2000)
        summary.removal_from_supply = _group_removals(rows)
    except OzonAPIError as e:
        logger.info("removal_from_supply_list failed: %s", e)

    return summary, errors


_REMOVAL_DONE_STATES = {
    # RU
    "завершено", "выдан", "выдана", "выданы", "получен", "получено",
    "утилизирован", "утилизировано", "возвращён", "возвращен", "отказ",
    # EN — на случай если Ozon переключит локаль
    "issued", "completed", "delivered_to_seller", "given_out",
    "disposed", "utilized", "cancelled", "returned",
}


def _group_removals(rows: List[Dict[str, Any]]) -> List[RemovalGroup]:
    """Свернуть rows в группы по (destination_warehouse, return_state).
    Юзеру важно только то, что СЕЙЧАС требует действия — поэтому отсекаем:
      • given_out_date / utilization_date проставлены;
      • return_state из «терминальных» (Завершено / Выдан / Утилизирован / ...);
      • дубликаты (box_id, offer_id) — Ozon-API часто возвращает одну коробку
        N раз построчно для каждого товара в ней.

    Сортировка: «в ПВЗ» сверху → «в пути» → старые сверху."""
    # Шаг 1: dedup. Ключ (box_id, offer_id) — Ozon API повторяет одни и те же
    # записи многократно (видели 75 одинаковых row → раздували items_count в 75 раз).
    seen: set = set()
    deduped: List[Dict[str, Any]] = []
    for r in rows:
        key = (r.get("box_id"), r.get("offer_id"), r.get("return_state"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)

    groups: Dict[tuple, Dict[str, Any]] = {}
    for r in deduped:
        # Уже выдан / утилизирован — пропускаем.
        if r.get("given_out_date") or r.get("utilization_date"):
            continue
        state = r.get("return_state") or "?"
        state_l = state.strip().lower()
        if state_l in _REMOVAL_DONE_STATES:
            continue
        wh = r.get("destination_warehouse_name") or "?"
        addr = r.get("destination_warehouse_address") or ""
        # «В ПВЗ» определяем по return_state, не по дате доставки (delivery_date
        # — это плановая дата, и она часто в прошлом для коробок, которые ещё в
        # пути). Ozon явно сигналит «Можно забирать всё» когда коробка реально
        # лежит в ПВЗ продавца.
        delivery_iso = r.get("delivery_date") or ""
        is_at_pvz = "забирать" in state_l or "прибы" in state_l or "доставлен" in state_l
        key = (wh, state, is_at_pvz)
        g = groups.setdefault(key, {
            "warehouse_name": wh, "warehouse_address": addr,
            "state": state, "is_at_pvz": is_at_pvz,
            "box_ids": set(), "items_count": 0,
            "delivery_dates": [],
            "sample_offer_ids": [],
        })
        bid = r.get("box_id")
        if bid:
            g["box_ids"].add(bid)
        g["items_count"] += int(r.get("quantity_for_return") or 0)
        if delivery_iso:
            g["delivery_dates"].append(delivery_iso)
        oid = r.get("offer_id")
        if oid and oid not in g["sample_offer_ids"] and len(g["sample_offer_ids"]) < 3:
            g["sample_offer_ids"].append(oid)
    out: List[RemovalGroup] = []
    for g in groups.values():
        # Самая близкая дата прибытия (если несколько — берём самую раннюю
        # для приоритета забрать).
        dd = sorted(g["delivery_dates"])[0] if g["delivery_dates"] else ""
        out.append(RemovalGroup(
            warehouse_name=g["warehouse_name"],
            warehouse_address=g["warehouse_address"],
            state=g["state"],
            is_at_pvz=g["is_at_pvz"],
            box_count=len(g["box_ids"]),
            items_count=g["items_count"],
            delivery_date=dd,
            sample_offer_ids=g["sample_offer_ids"],
        ))
    out.sort(key=lambda x: (not x.is_at_pvz, x.delivery_date or "9999"))
    return out


async def collect_sales_and_stocks(
    oz: OzonClient,
    *,
    days_window: int = 28,
) -> Tuple[Dict[int, Dict[str, Any]], List[str]]:
    """Источник — `POST /v1/analytics/stocks`. Ozon уже сам считает с учётом
    выкупаемости / OOS-мультипликатора / сезонности (см. data/screenshots/
    «принцип работы.txt»). Берём готовые метрики и агрегируем по SKU из всех
    кластеров:
        ads_total          — сумма ads по всем кластерам ≈ продажи/день суммарно
        idc_min            — самый «горячий» кластер (быстрее закончится)
        stock_total        — суммарный valid_stock_count
        requested_total    — сумма requested_stock_count = рекомендация Ozon
        worst_grade        — самая «красная» категория среди кластеров
    Возвращает: { sku: {stock, rate, days_left, to_ship, grade, name, offer_id} }.
    """
    errors: List[str] = []
    by_sku: Dict[int, Dict[str, Any]] = {}

    # 1. Список SKU + имена/offer_id берём из stocks_fbo — analytics/stocks
    # требует skus[] и не возвращает offer_id если в SKU нечего.
    sku_to_meta: Dict[int, Dict[str, str]] = {}
    sku_to_present: Dict[int, int] = {}
    try:
        stocks = await oz.stocks_fbo(limit=5000)
        for it in stocks:
            inner = it.get("stocks") or []
            fbo_entries = [s for s in inner if (s.get("type") == "fbo")]
            if not fbo_entries:
                continue
            sku_val = fbo_entries[0].get("sku")
            if not sku_val:
                continue
            try:
                sku = int(sku_val)
            except (ValueError, TypeError):
                continue
            sku_to_meta[sku] = {
                "name": it.get("name") or "",
                "offer_id": it.get("offer_id") or "",
            }
            sku_to_present[sku] = sum(int(s.get("present") or 0) for s in fbo_entries)
    except OzonAPIError as e:
        logger.warning("stocks_fbo failed: %s", e)
        errors.append(f"stocks_fbo: {str(e)[:200]}")

    if not sku_to_meta:
        return by_sku, errors

    # 2. analytics_stocks по 100 SKU за раз. На каждый SKU несколько cluster-row.
    all_items: List[Dict[str, Any]] = []
    sku_list = list(sku_to_meta.keys())
    chunk_size = 100
    for i in range(0, len(sku_list), chunk_size):
        chunk = [str(s) for s in sku_list[i:i + chunk_size]]
        try:
            items = await oz.analytics_stocks(chunk)
            all_items.extend(items)
        except OzonAPIError as e:
            logger.warning("analytics_stocks chunk %d failed: %s", i // chunk_size, e)
            errors.append(f"analytics_stocks: {str(e)[:200]}")

    # 3. Агрегация per SKU. Цветовая шкала Ozon → наша:
    # DEFICIT (<28д ожидаемый запас), POPULAR (28-56д), ACTUAL (56-120д),
    # SURPLUS (>120д), WAITING_FOR_SUPPLY (ждёт поставки от тебя),
    # WAS_* — «был X недавно», NO_SALES/COLLECTING_DATA — пропускаем.
    grade_priority = {
        # Меньше = «хуже» = краснее
        "DEFICIT": 0, "WAS_DEFICIT": 1,
        "POPULAR": 2, "WAS_POPULAR": 3,
        "WAITING_FOR_SUPPLY": 4,
        "ACTUAL": 5, "WAS_ACTUAL": 6,
        "SURPLUS": 7, "WAS_SURPLUS": 8,
        "NO_SALES": 9, "WAS_NO_SALES": 10,
        "RESTRICTED_NO_SALES": 11, "COLLECTING_DATA": 12,
        "UNSPECIFIED": 99, "TURNOVER_GRADE_NONE": 99,
    }

    for it in all_items:
        sku_val = it.get("sku")
        if not sku_val:
            continue
        try:
            sku = int(sku_val)
        except (ValueError, TypeError):
            continue
        meta = sku_to_meta.get(sku, {})
        entry = by_sku.setdefault(sku, {
            "name": meta.get("name") or it.get("name") or "",
            "offer_id": meta.get("offer_id") or it.get("offer_id") or "",
            "stock_total": 0,
            "rate": 0.0,
            "idc_min": None,
            "to_ship": 0,
            "worst_grade": None,
        })
        entry["stock_total"] += int(it.get("valid_stock_count") or 0)
        ads = it.get("ads")
        if isinstance(ads, (int, float)):
            entry["rate"] += float(ads)
        idc = it.get("idc")
        if isinstance(idc, (int, float)) and idc > 0:
            if entry["idc_min"] is None or idc < entry["idc_min"]:
                entry["idc_min"] = float(idc)
        entry["to_ship"] += int(it.get("requested_stock_count") or 0)
        grade = str(it.get("turnover_grade") or "").upper()
        if grade and grade in grade_priority:
            cur = entry["worst_grade"]
            if cur is None or grade_priority[grade] < grade_priority.get(cur, 99):
                entry["worst_grade"] = grade

    # Если у SKU не было ни одной cluster-row с idc — берём текущий valid_stock
    # как fallback (но days_left=inf, чтобы не попасть в urgent).
    for sku, e in by_sku.items():
        if e["stock_total"] == 0:
            e["stock_total"] = sku_to_present.get(sku, 0)

    return by_sku, errors


def _build_lines(by_sku: Dict[int, Dict[str, Any]], *, mode: str) -> List[SkuLine]:
    """Источник данных — `/v1/analytics/stocks` (см. collect_sales_and_stocks).

    mode='urgent' — SKU с requested_stock_count > 0 (Ozon рекомендует докинуть).
        Цвет по worst_grade: DEFICIT/WAS_DEFICIT → 🔴, POPULAR/WAS_POPULAR → 🟡.
        Сортировка по to_ship ↓.
    mode='runout' — остальные SKU с idc ≤ 30 (быстро закончится).
        🔴 < 15д, 🟡 15-30д, 🟢 > 30д.
        Сортировка по idc ↑.
    SKU с WAITING_FOR_SUPPLY / NO_SALES / COLLECTING_DATA пропускаем — там
    Ozon ещё не может посчитать, шум."""
    skip_grades = {
        "WAITING_FOR_SUPPLY", "NO_SALES", "WAS_NO_SALES",
        "RESTRICTED_NO_SALES", "COLLECTING_DATA",
        "UNSPECIFIED", "TURNOVER_GRADE_NONE",
    }
    red_grades = {"DEFICIT", "WAS_DEFICIT"}
    yellow_grades = {"POPULAR", "WAS_POPULAR"}

    lines: List[SkuLine] = []
    for sku, e in by_sku.items():
        stock = int(e.get("stock_total") or 0)
        rate = float(e.get("rate") or 0.0)
        idc = e.get("idc_min")
        to_ship = int(e.get("to_ship") or 0)
        grade = str(e.get("worst_grade") or "")

        if grade in skip_grades and to_ship == 0:
            continue

        if mode == "urgent":
            if to_ship <= 0:
                continue
            if grade in red_grades:
                color = "🔴"
            elif grade in yellow_grades:
                color = "🟡"
            elif idc is not None and idc < URGENT_RED_DAYS:
                color = "🔴"
            elif idc is not None and idc < URGENT_YELLOW_DAYS:
                color = "🟡"
            else:
                color = "🟡"  # дефолт для прочих с requested > 0
        else:  # runout
            if to_ship > 0:
                # уже показали в urgent — не дублируем
                continue
            if idc is None or idc <= 0:
                continue
            if idc > RUNOUT_GREEN_DAYS:
                color = "🟢"
            elif idc < RUNOUT_RED_DAYS:
                color = "🔴"
            else:
                color = "🟡"

        days_left = float(idc) if idc is not None else float("inf")
        lines.append(SkuLine(
            sku=sku,
            offer_id=str(e.get("offer_id") or ""),
            name=str(e.get("name") or "")[:50],
            stock=stock,
            rate_per_day=rate,
            days_left=days_left,
            color=color,
            to_ship_qty=to_ship,
        ))

    if mode == "urgent":
        lines.sort(key=lambda x: (-x.to_ship_qty, x.days_left))
    else:
        lines.sort(key=lambda x: x.days_left)
    return lines


async def collect_acts_awaiting(oz: OzonClient) -> Tuple[List[ActAwaitingItem], List[str]]:
    """Поставки в состоянии REPORTS_CONFIRMATION_AWAITING / REPORT_REJECTED —
    юзеру нужно подтвердить акт в Ozon ЛК (иначе через 5 дней автоподтверждение
    того что заявил Ozon, без шанса оспорить расхождения)."""
    errors: List[str] = []
    out: List[ActAwaitingItem] = []
    try:
        order_ids = await oz.supply_order_list(
            states=["REPORTS_CONFIRMATION_AWAITING", "REPORT_REJECTED"],
            max_total=200,
        )
    except OzonAPIError as e:
        logger.warning("supply_order_list (acts) failed: %s", e)
        errors.append(f"supply_order_list: {str(e)[:200]}")
        return out, errors
    if not order_ids:
        return out, errors
    try:
        # supply_order_get берёт до 50 за раз, бьём пачками.
        for i in range(0, len(order_ids), 50):
            chunk = order_ids[i:i + 50]
            orders = await oz.supply_order_get(chunk)
            for o in orders:
                state = str(o.get("state") or "")
                if state not in ("REPORTS_CONFIRMATION_AWAITING", "REPORT_REJECTED"):
                    continue
                # API использует drop_off_warehouse (с подчёркиваниями),
                # не dropoff_warehouse — раньше был баг, имя склада → "?".
                drop = o.get("drop_off_warehouse") or o.get("dropoff_warehouse") or {}
                out.append(ActAwaitingItem(
                    order_id=int(o.get("order_id") or 0),
                    order_number=str(o.get("order_number") or ""),
                    state=state,
                    dropoff_name=str(drop.get("name") or ""),
                    state_updated_at=str(o.get("state_updated_date") or ""),
                ))
    except OzonAPIError as e:
        logger.warning("supply_order_get (acts) failed: %s", e)
        errors.append(f"supply_order_get: {str(e)[:200]}")
    out.sort(key=lambda a: a.state_updated_at)  # старые наверху (срочнее)
    return out, errors


async def collect_digest(oz: OzonClient) -> DigestData:
    """Главная точка сборки сводки."""
    data = DigestData(generated_at=datetime.now(timezone.utc))

    returns, ret_errors = await collect_returns_summary(oz)
    data.returns = returns
    data.errors.extend(ret_errors)

    acts, acts_errors = await collect_acts_awaiting(oz)
    data.acts_awaiting = acts
    data.errors.extend(acts_errors)

    by_sku, sales_errors = await collect_sales_and_stocks(oz, days_window=28)
    data.errors.extend(sales_errors)

    data.urgent = _build_lines(by_sku, mode="urgent")
    data.runout = _build_lines(by_sku, mode="runout")

    return data


# ── Рендер текста ─────────────────────────────────────────────────────────

def _fmt_removal_group(g: "RemovalGroup") -> str:
    """Одна группа вывоза для рендера в digest. Кружок:
    🔴 — уже в ПВЗ (забрать!), 🟡 — в пути.
    Артикулы НЕ показываем — юзеру важно куда ехать и сколько забрать."""
    icon = "🔴" if g.is_at_pvz else "🟡"
    state_prefix = "в ПВЗ" if g.is_at_pvz else "в пути"
    when_part = f" · {state_prefix}"
    if g.delivery_date:
        try:
            dt = datetime.fromisoformat(g.delivery_date.replace("Z", "+00:00"))
            suffix = " с " if g.is_at_pvz else ", ожидается "
            when_part = f" · {state_prefix}{suffix}{dt.strftime('%d.%m')}"
        except (ValueError, TypeError):
            pass
    addr_part = ""
    if g.warehouse_address:
        # Полный адрес — юзеру нужно знать куда ехать. 60 символов было мало,
        # обрезалось посреди фразы («городской округ Домодедово, дере…»).
        addr_part = f"\n     <i>{g.warehouse_address}</i>"
    return (
        f"{icon} <b>{g.warehouse_name}</b> — {g.box_count} кор., "
        f"{g.items_count} шт{when_part}{addr_part}"
    )


def _fmt_sku_line(line: SkuLine, *, show_days: bool = True) -> str:
    """Три строки на SKU — чтобы не сливалось в кашу на узких экранах:
      1) {эмодзи} {название}
      2) {артикул в моноширине}
      3) {остаток · скорость · дни → к отгрузке (если urgent)}
    """
    name = line.name or f"SKU {line.sku}"
    head = f"{line.color} <b>{name}</b>"
    article_part = f"\n   <code>{line.offer_id}</code>" if line.offer_id else ""
    days = "∞" if line.days_left == float("inf") else f"{line.days_left:.0f}д"
    ship_part = f" · <b>отгрузить {line.to_ship_qty}</b>" if line.to_ship_qty > 0 else ""
    if show_days:
        tail = f"\n   ост. {line.stock} · {line.rate_per_day:.1f}/д → {days}{ship_part}"
    else:
        tail = f"\n   ост. {line.stock} · {line.rate_per_day:.1f}/д{ship_part}"
    return f"{head}{article_part}{tail}"


def build_digest_text(data: DigestData) -> str:
    """HTML-сводка для отправки в Telegram."""
    msk_now = data.generated_at + timedelta(hours=3)
    lines: List[str] = [
        f"☀ <b>Утренняя сводка</b> · {msk_now.strftime('%d.%m %H:%M')} МСК",
        "",
    ]

    # Акты, ждущие подтверждения (главное сверху — срочные deadline'ы Ozon).
    if data.acts_awaiting:
        lines.append("📋 <b>Акты ждут подтверждения</b>")
        lines.append(
            "<i>Ozon принял поставки, но без твоего подтверждения через ~5 дней "
            "цифры замораживаются автоматически. Открой каждую и сверь приход.</i>"
        )
        for a in data.acts_awaiting[:10]:
            state_icon = "🔴" if a.state == "REPORT_REJECTED" else "🟡"
            state_text = "акт отклонён" if a.state == "REPORT_REJECTED" else "ждёт подтверждения"
            when = ""
            if a.state_updated_at:
                try:
                    dt = datetime.fromisoformat(a.state_updated_at.replace("Z", "+00:00"))
                    when = f" · с {dt.strftime('%d.%m')}"
                except (ValueError, TypeError):
                    pass
            order_url = f"https://seller.ozon.ru/app/supply/orders/{a.order_id}"
            lines.append(
                f'{state_icon} <a href="{order_url}"><b>#{a.order_number}</b></a> · '
                f"{a.dropoff_name or '?'} · {state_text}{when}"
            )
        if len(data.acts_awaiting) > 10:
            lines.append(f"  …и ещё {len(data.acts_awaiting) - 10}")
        lines.append(
            '🔗 <a href="https://seller.ozon.ru/app/supply/orders?filter=ReportsConfirmation">'
            "Открыть список в Ozon ЛК</a>"
        )
        lines.append("")

    # Возвраты — это товары к получению в ПВЗ + giveout-партии. Removals
    # (вывозы со стока FBO) — отдельная сущность ниже, в этом блоке их не
    # упоминаем. PDF этикетка относится к returns/giveouts, не к removals.
    r = data.returns
    lines.append("📥 <b>Возвраты Ozon</b>")
    has_returns = bool(r.total or r.giveouts_available or r.giveouts_at_pvz)
    if not has_returns:
        lines.append("✅ Нет возвратов к получению.")
    else:
        if r.total:
            lines.append(f"  • На ПВЗ ждут получения: <b>{r.total}</b>")
        if r.giveouts_available:
            lines.append(f"  • 📦 Партии к вывозу с FBO: <b>{r.giveouts_available}</b>")
        if r.giveouts_at_pvz:
            lines.append(f"  • ✅ Партии уже в ПВЗ продавца: <b>{r.giveouts_at_pvz}</b>")
        if r.pdf_bytes:
            lines.append("  • 📄 Этикетка партии — отдельным сообщением ниже.")
    lines.append("")

    # Вывозы со стока FBO (товар, который продавец заказал вывезти со склада).
    if r.removal_from_stock:
        lines.append("📤 <b>Вывозы со стока FBO</b>")
        for g in r.removal_from_stock[:8]:
            lines.append(_fmt_removal_group(g))
        if len(r.removal_from_stock) > 8:
            lines.append(f"  …и ещё {len(r.removal_from_stock) - 8} групп")
        lines.append("")

    # Вывозы с поставки (отбраковка приёмки).
    if r.removal_from_supply:
        lines.append("📤 <b>Вывозы с поставки</b> <i>(отбраковка приёмки)</i>")
        for g in r.removal_from_supply[:8]:
            lines.append(_fmt_removal_group(g))
        if len(r.removal_from_supply) > 8:
            lines.append(f"  …и ещё {len(r.removal_from_supply) - 8} групп")
        lines.append("")

    # Срочно отгрузить — топ-5 по требуемому количеству к отгрузке.
    # Источник — `/v1/analytics/stocks` Ozon: ads (ср. продажи/день),
    # idc (дни покрытия), requested_stock_count (рекомендация).
    # Ozon учитывает наличие/заказы; сезонность отдельной аналитической
    # системы (data.ozon) тут НЕ применяется — это другой сервис.
    header_count = min(TOP_URGENT_LIMIT, len(data.urgent))
    lines.append(
        f"🔥 <b>Топ-{header_count} срочно отгрузить</b> "
        f"<i>(рекомендация Ozon из аналитики остатков)</i>"
    )
    if not data.urgent:
        lines.append("✅ Запасов хватает — паника отменяется.")
    else:
        for line in data.urgent[:TOP_URGENT_LIMIT]:
            lines.append(_fmt_sku_line(line))
        if len(data.urgent) > TOP_URGENT_LIMIT:
            lines.append(f"  …и ещё {len(data.urgent) - TOP_URGENT_LIMIT} в очереди")
    lines.append("")

    # Runout — топ-5 🔴 + топ-3 🟡 + счётчик 🟢. Без подзаголовков-секций:
    # эмодзи слева у каждой строки уже сообщает «красная»/«жёлтая» зону.
    lines.append("⏳ <b>Кончатся</b> <i>(дни покрытия по прогнозу Ozon)</i>")
    if not data.runout:
        lines.append("ℹ Нет данных по продажам — добавь товары в каталог Ozon или жди заказов.")
    else:
        red = [l for l in data.runout if l.color == "🔴"]
        yellow = [l for l in data.runout if l.color == "🟡"]
        green = [l for l in data.runout if l.color == "🟢"]
        shown_red = red[:TOP_RUNOUT_RED_LIMIT]
        for line in shown_red:
            lines.append(_fmt_sku_line(line))
        if len(red) > TOP_RUNOUT_RED_LIMIT:
            lines.append(f"  …и ещё 🔴 {len(red) - TOP_RUNOUT_RED_LIMIT}")
        shown_yellow = yellow[:TOP_RUNOUT_YELLOW_LIMIT]
        for line in shown_yellow:
            lines.append(_fmt_sku_line(line))
        if len(yellow) > TOP_RUNOUT_YELLOW_LIMIT:
            lines.append(f"  …и ещё 🟡 {len(yellow) - TOP_RUNOUT_YELLOW_LIMIT}")
        if green:
            lines.append(f"🟢 ещё {len(green)} SKU с запасом &gt; {RUNOUT_GREEN_DAYS} дн")

    if data.errors:
        lines.append("")
        lines.append("<i>⚠ Не всё API ответило:</i>")
        for err in data.errors:
            lines.append(f"  · <code>{err[:160]}</code>")

    return "\n".join(lines)
