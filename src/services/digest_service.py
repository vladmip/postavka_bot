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
TOP_LIST_LIMIT = 12        # сколько SKU показывать в каждом списке


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


def _group_removals(rows: List[Dict[str, Any]]) -> List[RemovalGroup]:
    """Свернуть rows в группы по (destination_warehouse, return_state).
    Скрываем уже выданные (given_out_date != null) и утилизированные —
    юзеру важно то, что СЕЙЧАС требует действия.
    Сортировка: «в ПВЗ» сверху → «в пути» → старые сверху."""
    groups: Dict[tuple, Dict[str, Any]] = {}
    for r in rows:
        # Уже выдан в ПВЗ юзеру — пропускаем (или утилизирован).
        if r.get("given_out_date") or r.get("utilization_date"):
            continue
        wh = r.get("destination_warehouse_name") or "?"
        addr = r.get("destination_warehouse_address") or ""
        state = r.get("return_state") or "?"
        # Эвристика «в ПВЗ»: дата прибытия в прошлом (delivery_date <= today),
        # но выдачи нет (given_out_date пустой — мы их уже отфильтровали).
        delivery_iso = r.get("delivery_date") or ""
        is_at_pvz = False
        if delivery_iso:
            try:
                dt = datetime.fromisoformat(delivery_iso.replace("Z", "+00:00"))
                is_at_pvz = dt <= datetime.now(timezone.utc)
            except (ValueError, TypeError):
                pass
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
    """Тянет stocks_fbo + postings_fbo за `days_window` дней.

    Возвращает: { sku: {stock, sold_7d, sold_28d, name, offer_id} } + список ошибок.
    """
    errors: List[str] = []
    by_sku: Dict[int, Dict[str, Any]] = {}

    # 1. Остатки FBO. /v4/product/info/stocks возвращает items с подсписком
    # stocks[{type: fbo|fbs|rfbs, present, sku, ...}]. SKU привязан к подсписку,
    # а НЕ к item верхнего уровня — там только offer_id/product_id. Раньше брал
    # product_id, ключ не совпадал с posting.products[].sku → by_sku пустой.
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
            present = sum(int(s.get("present") or 0) for s in fbo_entries)
            entry = by_sku.setdefault(sku, {
                "stock": 0, "sold_7d": 0, "sold_28d": 0,
                "name": it.get("name") or "", "offer_id": it.get("offer_id") or "",
            })
            entry["stock"] = present
            if it.get("name"):
                entry["name"] = it["name"]
            if it.get("offer_id"):
                entry["offer_id"] = it["offer_id"]
    except OzonAPIError as e:
        logger.warning("stocks_fbo failed: %s", e)
        errors.append(f"stocks_fbo: {str(e)[:200]}")

    # 2. Заказы FBO за окно
    now = datetime.now(timezone.utc)
    date_from = (now - timedelta(days=days_window)).strftime("%Y-%m-%dT00:00:00.000Z")
    date_to = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    cutoff_7d = now - timedelta(days=7)

    try:
        postings = await oz.postings_fbo_list(date_from, date_to, max_total=20000)
    except OzonAPIError as e:
        logger.warning("postings_fbo_list failed: %s", e)
        errors.append(f"postings_fbo_list: {str(e)[:200]}")
        return by_sku, errors

    for p in postings:
        status = str(p.get("status") or "").lower()
        if status == "cancelled":
            continue
        in_process = p.get("in_process_at") or ""
        try:
            ts = datetime.fromisoformat(in_process.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            ts = None
        is_recent = bool(ts and ts >= cutoff_7d)
        for prod in p.get("products") or []:
            sku_val = prod.get("sku")
            if not sku_val:
                continue
            try:
                sku = int(sku_val)
            except (ValueError, TypeError):
                continue
            qty = int(prod.get("quantity") or 0)
            if qty <= 0:
                continue
            entry = by_sku.setdefault(sku, {
                "stock": 0, "sold_7d": 0, "sold_28d": 0,
                "name": prod.get("name") or "", "offer_id": prod.get("offer_id") or "",
            })
            entry["sold_28d"] += qty
            if is_recent:
                entry["sold_7d"] += qty
            if not entry["name"] and prod.get("name"):
                entry["name"] = prod["name"]
            if not entry["offer_id"] and prod.get("offer_id"):
                entry["offer_id"] = prod["offer_id"]

    return by_sku, errors


def _build_lines(by_sku: Dict[int, Dict[str, Any]], *, mode: str) -> List[SkuLine]:
    """mode='urgent' → срочно отгрузить (по 7d-rate), 🔴 < 7д, 🟡 < 14д, остальное скрываем.
    mode='runout' → когда кончится (по 28d-rate), 🔴 < 15д, 🟡 < 30д, 🟢 > 30д.
    Товары с stock=0 пропускаем — они уже закончились, в «срочно/кончатся» бессмысленно."""
    lines: List[SkuLine] = []
    for sku, e in by_sku.items():
        stock = int(e.get("stock") or 0)
        if stock <= 0:
            continue  # уже закончилось — не «срочно», просто факт
        if mode == "urgent":
            sold = e.get("sold_7d") or 0
            rate = sold / 7.0
            if rate <= 0:
                continue
            days_left = stock / rate if rate > 0 else float("inf")
            if days_left < URGENT_RED_DAYS:
                color = "🔴"
            elif days_left < URGENT_YELLOW_DAYS:
                color = "🟡"
            else:
                continue
        else:  # runout
            sold = e.get("sold_28d") or 0
            rate = sold / 28.0
            if rate <= 0:
                continue
            days_left = stock / rate if rate > 0 else float("inf")
            if days_left < RUNOUT_RED_DAYS:
                color = "🔴"
            elif days_left <= RUNOUT_GREEN_DAYS:
                color = "🟡"
            else:
                color = "🟢"
        lines.append(SkuLine(
            sku=sku,
            offer_id=str(e.get("offer_id") or ""),
            name=str(e.get("name") or "")[:50],
            stock=stock,
            rate_per_day=rate,
            days_left=days_left,
            color=color,
        ))
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
    🔴 — уже в ПВЗ (забрать!), 🟡 — в пути."""
    icon = "🔴" if g.is_at_pvz else "🟡"
    when_part = ""
    if g.delivery_date:
        try:
            dt = datetime.fromisoformat(g.delivery_date.replace("Z", "+00:00"))
            when_part = (" · в ПВЗ с " if g.is_at_pvz else " · ожидается ") + dt.strftime("%d.%m")
        except (ValueError, TypeError):
            pass
    sample = (
        f" · <code>{','.join(g.sample_offer_ids)}</code>"
        if g.sample_offer_ids else ""
    )
    addr_part = ""
    if g.warehouse_address:
        # Полный адрес — юзеру нужно знать куда ехать. 60 символов было мало,
        # обрезалось посреди фразы («городской округ Домодедово, дере…»).
        addr_part = f"\n     <i>{g.warehouse_address}</i>"
    return (
        f"{icon} <b>{g.warehouse_name}</b> — {g.box_count} кор., "
        f"{g.items_count} шт{when_part}{sample}{addr_part}"
    )


def _fmt_sku_line(line: SkuLine, *, show_days: bool = True) -> str:
    name = line.name or f"SKU {line.sku}"
    label = f"<b>{name}</b>"
    if line.offer_id:
        label += f" <code>{line.offer_id}</code>"
    if show_days:
        days = "∞" if line.days_left == float("inf") else f"{line.days_left:.0f}д"
        return (
            f"{line.color} {label}\n"
            f"   ост. {line.stock} · {line.rate_per_day:.1f}/д → {days}"
        )
    return (
        f"{line.color} {label}\n"
        f"   ост. {line.stock} · {line.rate_per_day:.1f}/д"
    )


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

    # Возвраты
    r = data.returns
    lines.append("📥 <b>Возвраты Ozon</b>")
    nothing = (
        r.total == 0 and r.giveouts_available == 0 and r.giveouts_at_pvz == 0
        and not r.removal_from_stock and not r.removal_from_supply
    )
    if nothing:
        lines.append("✅ Всё пусто — забирать нечего.")
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

    # Срочно отгрузить
    lines.append("🔥 <b>Срочно отгрузить</b> <i>(по продажам за 7 дней)</i>")
    if not data.urgent:
        lines.append("✅ Запасов хватает — паника отменяется.")
    else:
        for line in data.urgent[:TOP_LIST_LIMIT]:
            lines.append(_fmt_sku_line(line))
        if len(data.urgent) > TOP_LIST_LIMIT:
            lines.append(f"  …и ещё {len(data.urgent) - TOP_LIST_LIMIT}")
    lines.append("")

    # Runout
    lines.append("⏳ <b>Кончатся</b> <i>(по продажам за 28 дней)</i>")
    if not data.runout:
        lines.append("ℹ Нет данных по продажам — добавь товары в каталог Ozon или жди заказов.")
    else:
        red = [l for l in data.runout if l.color == "🔴"]
        yellow = [l for l in data.runout if l.color == "🟡"]
        green = [l for l in data.runout if l.color == "🟢"]
        if red:
            lines.append(f"<b>🔴 &lt; {RUNOUT_RED_DAYS} дн</b> ({len(red)})")
            for line in red[:TOP_LIST_LIMIT]:
                lines.append(_fmt_sku_line(line))
            if len(red) > TOP_LIST_LIMIT:
                lines.append(f"  …и ещё {len(red) - TOP_LIST_LIMIT}")
        if yellow:
            lines.append(f"<b>🟡 {RUNOUT_RED_DAYS}–{RUNOUT_GREEN_DAYS} дн</b> ({len(yellow)})")
            for line in yellow[:TOP_LIST_LIMIT // 2]:
                lines.append(_fmt_sku_line(line))
            if len(yellow) > TOP_LIST_LIMIT // 2:
                lines.append(f"  …и ещё {len(yellow) - TOP_LIST_LIMIT // 2}")
        if green:
            lines.append(f"<b>🟢 &gt; {RUNOUT_GREEN_DAYS} дн</b>: {len(green)} SKU")

    if data.errors:
        lines.append("")
        lines.append("<i>⚠ Не всё API ответило:</i>")
        for err in data.errors:
            lines.append(f"  · <code>{err[:160]}</code>")

    return "\n".join(lines)
