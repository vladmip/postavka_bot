"""Сервис для заявок на отгрузку (ShipmentRequest)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

from sqlalchemy.orm import Session

from src.db.models import ShipmentRequest, ShipmentItem, Sku, OzonProduct, WbProduct
from src.parsers.ship_request import ShipFileParsed

logger = logging.getLogger("services.shipment")


@dataclass
class AttachResult:
    request_id: int
    matched: int          # сколько ShipItem нашли SKU в каталоге
    unmatched_articles: List[str]   # raw artсuls без пары
    items_added: int      # сколько строк добавлено в shipment_items
    cluster: str
    marketplace: str


def _find_ozon_product(session: Session, raw: str) -> Optional[OzonProduct]:
    """Поиск OzonProduct по raw_article из xlsx:
    1) точное равенство offer_id
    2) нормализованное сравнение (lowercase + кириллица→латиница)
    3) fallback по barcode_primary
    """
    from src.services.catalog_service import normalize_for_match
    raw = (raw or "").strip()
    if not raw:
        return None
    # 1) Точное по offer_id
    p = session.query(OzonProduct).filter(OzonProduct.offer_id == raw).first()
    if p:
        return p
    # 2) Нормализованное
    target = normalize_for_match(raw)
    if not target:
        return None
    for p in session.query(OzonProduct).all():
        if normalize_for_match(p.offer_id) == target:
            return p
        if p.barcode_primary and normalize_for_match(p.barcode_primary) == target:
            return p
    return None


def _find_wb_product(session: Session, raw: str) -> Optional[WbProduct]:
    """Поиск WbProduct по raw_article из xlsx: точно/нормализовано по article и barcode."""
    from src.services.catalog_service import normalize_for_match
    raw = (raw or "").strip()
    if not raw:
        return None
    # Точное по article
    p = session.query(WbProduct).filter(WbProduct.article == raw).first()
    if p:
        return p
    target = normalize_for_match(raw)
    if not target:
        return None
    for p in session.query(WbProduct).all():
        if p.article and normalize_for_match(p.article) == target:
            return p
        if p.barcode_primary and normalize_for_match(p.barcode_primary) == target:
            return p
    return None


def create_shipment_request(session: Session, source_file: str) -> ShipmentRequest:
    req = ShipmentRequest(
        state="draft",
        source_files_json=[source_file],
        crossdock_warehouses_json={},
    )
    session.add(req)
    session.flush()
    return req


def attach_ship_file(
    session: Session,
    request_id: int,
    parsed: ShipFileParsed,
) -> AttachResult:
    """Добавить позиции из распарсенного файла в заявку."""
    req = session.get(ShipmentRequest, request_id)
    if not req:
        raise ValueError(f"ShipmentRequest #{request_id} не найден")

    # Запомним имя файла
    files = list(req.source_files_json or [])
    if parsed.file_name and parsed.file_name not in files:
        files.append(parsed.file_name)
        req.source_files_json = files

    matched = 0
    unmatched: List[str] = []
    added = 0
    mp = (parsed.marketplace or "").lower()
    for it in parsed.items:
        ozon_pid = None
        wb_pid = None
        if mp == "ozon":
            p = _find_ozon_product(session, it.article_or_barcode)
            if p:
                ozon_pid = p.id
                matched += 1
            else:
                unmatched.append(it.article_or_barcode)
        elif mp == "wb":
            p = _find_wb_product(session, it.article_or_barcode)
            if p:
                wb_pid = p.id
                matched += 1
            else:
                unmatched.append(it.article_or_barcode)
        else:
            unmatched.append(it.article_or_barcode)
        session.add(ShipmentItem(
            request_id=req.id,
            ozon_product_id=ozon_pid,
            wb_product_id=wb_pid,
            raw_article=it.article_or_barcode,
            marketplace=parsed.marketplace,
            cluster=parsed.cluster_name,
            qty=it.qty,
        ))
        added += 1

    return AttachResult(
        request_id=req.id,
        matched=matched,
        unmatched_articles=unmatched,
        items_added=added,
        cluster=parsed.cluster_name,
        marketplace=parsed.marketplace,
    )


def list_shipment_requests(session: Session, limit: int = 30) -> List[ShipmentRequest]:
    return (
        session.query(ShipmentRequest)
        .order_by(ShipmentRequest.id.desc())
        .limit(limit)
        .all()
    )


def get_shipment_request(session: Session, request_id: int) -> Optional[ShipmentRequest]:
    return session.get(ShipmentRequest, request_id)


def refresh_request_state_after_booking(req: ShipmentRequest) -> None:
    """Пересчитать req.state после изменения booked_supply_id у items.
    «supplies_created» ставим ТОЛЬКО когда ВСЕ items имеют booked_supply_id —
    иначе заявка остаётся в текущем состоянии (slot_searching / planning), чтобы
    юзер мог докрутить остальные кластеры."""
    if not req.items:
        return
    all_booked = all(bool(it.booked_supply_id) for it in req.items)
    if all_booked:
        req.state = "supplies_created"
    elif req.state == "supplies_created":
        # Регрессия (например, юзер сбросил один booking) — вернуть в slot_searching.
        req.state = "slot_searching"


def shipment_summary(req: ShipmentRequest) -> str:
    """Краткая сводка по заявке (по кластерам) с пометками booking-статуса.

    Каждый кластер — ✅ если все его items booked, ⏳ если ни один не booked,
    🟡 если booked частично. Для booked-кластеров показываем order_id и время
    слота (берём из первого item с booked_supply_id)."""
    by_cluster: dict = {}
    for it in req.items:
        key = (it.marketplace, it.cluster)
        st = by_cluster.setdefault(key, {
            "qty": 0, "skus": 0, "unmatched": 0,
            "booked": 0, "order_id": None, "warehouse": None, "slot_at": None,
        })
        st["qty"] += it.qty
        st["skus"] += 1
        mp = (it.marketplace or "").lower()
        unmatched = (mp == "ozon" and not it.ozon_product_id) or \
                    (mp == "wb" and not it.wb_product_id)
        if unmatched:
            st["unmatched"] += 1
        if it.booked_supply_id:
            st["booked"] += 1
            if not st["order_id"]:
                st["order_id"] = it.booked_supply_id
                st["warehouse"] = it.target_warehouse
                st["slot_at"] = it.booked_slot_at

    lines = []
    for (mp, cluster), stat in sorted(by_cluster.items()):
        if stat["booked"] >= stat["skus"]:
            mark = "✅"
        elif stat["booked"] > 0:
            mark = "🟡"
        else:
            mark = "⏳"
        unm = f" ⚠ {stat['unmatched']} без SKU" if stat["unmatched"] else ""
        base = (
            f"  {mark} {mp.upper()} «{cluster}»: "
            f"{stat['skus']} SKU, {stat['qty']} шт{unm}"
        )
        if stat["order_id"]:
            slot_s = ""
            if stat["slot_at"]:
                slot_s = f" · {stat['slot_at']:%d.%m %H:%M}"
            wh_s = f" · {stat['warehouse']}" if stat["warehouse"] else ""
            base += f"\n      order_id {stat['order_id']}{wh_s}{slot_s}"
        lines.append(base)
    return "\n".join(lines) if lines else "(пусто)"
