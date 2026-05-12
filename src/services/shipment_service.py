"""Сервис для заявок на отгрузку (ShipmentRequest)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

from sqlalchemy.orm import Session

from src.db.models import ShipmentRequest, ShipmentItem, Sku
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


def _find_sku(session: Session, raw: str) -> Optional[Sku]:
    """SKU поиск по barcode → article (case-insensitive)."""
    raw = raw.strip()
    # 1) точное совпадение barcode (часто 13 цифр)
    sku = session.query(Sku).filter(Sku.barcode == raw).first()
    if sku:
        return sku
    # 2) точное совпадение article
    sku = session.query(Sku).filter(Sku.article == raw).first()
    if sku:
        return sku
    # 3) case-insensitive article (на случай '3CHOС' vs '3CHOC' — кириллический С)
    sku = (
        session.query(Sku)
        .filter(Sku.article.ilike(raw))
        .first()
    )
    if sku:
        return sku
    # 4) попытка: убрать любые не-ASCII (на случай смешанной кириллицы)
    ascii_raw = raw.encode("ascii", errors="replace").decode("ascii").replace("?", "")
    if ascii_raw and ascii_raw != raw:
        sku = (
            session.query(Sku)
            .filter(Sku.article.ilike(ascii_raw))
            .first()
        )
        if sku:
            return sku
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
    for it in parsed.items:
        sku = _find_sku(session, it.article_or_barcode)
        if sku:
            matched += 1
        else:
            unmatched.append(it.article_or_barcode)
        session.add(ShipmentItem(
            request_id=req.id,
            sku_id=sku.id if sku else None,
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


def shipment_summary(req: ShipmentRequest) -> str:
    """Краткая сводка по заявке (по кластерам)."""
    by_cluster: dict = {}
    for it in req.items:
        key = (it.marketplace, it.cluster)
        by_cluster.setdefault(key, {"qty": 0, "skus": 0, "unmatched": 0})
        by_cluster[key]["qty"] += it.qty
        by_cluster[key]["skus"] += 1
        if it.sku_id is None:
            by_cluster[key]["unmatched"] += 1

    lines = []
    for (mp, cluster), stat in sorted(by_cluster.items()):
        unm = f" ⚠ {stat['unmatched']} без SKU" if stat["unmatched"] else ""
        lines.append(
            f"  {mp.upper()} «{cluster}»: {stat['skus']} SKU, {stat['qty']} шт{unm}"
        )
    return "\n".join(lines) if lines else "(пусто)"
