"""CRUD поставок + переходы состояний."""
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from src.db.models import Supply, SupplyItem, StateLog, Sku, SUPPLY_STATES
from src.services.catalog_service import expand_kit, get_kit_components, get_sku


def create_supply(session: Session, marketplace: str, warehouse: str, comments: str = "") -> Supply:
    s = Supply(marketplace=marketplace, warehouse=warehouse, state="draft", comments=comments or None)
    session.add(s)
    session.flush()
    log_state_change(session, s.id, None, "draft", "supply created")
    return s


def get_supply(session: Session, supply_id: int) -> Optional[Supply]:
    return session.scalar(
        select(Supply)
        .where(Supply.id == supply_id)
        .options(selectinload(Supply.items).selectinload(SupplyItem.sku))
    )


def list_supplies(session: Session, state: Optional[str] = None, limit: int = 50) -> List[Supply]:
    q = select(Supply).order_by(Supply.created_at.desc()).limit(limit)
    if state:
        q = q.where(Supply.state == state)
    return list(session.scalars(q))


def add_item(
    session: Session, supply_id: int, sku_id: int, qty_planned: int, expand: bool = True
) -> List[SupplyItem]:
    """Добавить позицию. Если sku — kit и expand=True, добавляются компоненты с пометкой expanded_from."""
    sku = get_sku(session, sku_id)
    if not sku:
        raise ValueError(f"sku_id={sku_id} not found")

    components = get_kit_components(session, sku_id) if expand else []
    items: List[SupplyItem] = []

    if components:
        # пишем kit как информационную строку
        kit_item = SupplyItem(supply_id=supply_id, sku_id=sku_id, qty_planned=qty_planned)
        session.add(kit_item)
        items.append(kit_item)
        for csku, cqty in expand_kit(session, sku_id, qty_planned):
            comp_item = SupplyItem(
                supply_id=supply_id,
                sku_id=csku.id,
                qty_planned=cqty,
                expanded_from_kit_id=sku_id,
            )
            session.add(comp_item)
            items.append(comp_item)
    else:
        item = SupplyItem(supply_id=supply_id, sku_id=sku_id, qty_planned=qty_planned)
        session.add(item)
        items.append(item)

    session.flush()
    return items


def transition(session: Session, supply_id: int, new_state: str, event: str = "") -> Supply:
    if new_state not in SUPPLY_STATES:
        raise ValueError(f"unknown state: {new_state}")
    supply = session.get(Supply, supply_id)
    if not supply:
        raise ValueError(f"supply_id={supply_id} not found")
    old = supply.state
    if old == new_state:
        return supply
    supply.state = new_state
    log_state_change(session, supply_id, old, new_state, event)
    return supply


def log_state_change(
    session: Session, supply_id: int, from_state: Optional[str], to_state: str, event: str
) -> None:
    log = StateLog(supply_id=supply_id, from_state=from_state, to_state=to_state, event_text=event or None)
    session.add(log)


def attach_picked_qty(session: Session, supply_id: int, opis_items: List[tuple]) -> dict:
    """opis_items: List[(barcode_or_article, qty, box_label, expiry)].
    Заполняет qty_picked / box_label / expiry в supply_items по соответствию article или barcode.
    Возвращает {'matched': int, 'missing': [str]}.
    """
    supply = get_supply(session, supply_id)
    if not supply:
        raise ValueError(f"supply_id={supply_id} not found")

    by_article = {it.sku.article: it for it in supply.items if it.sku}
    by_barcode = {it.sku.barcode: it for it in supply.items if it.sku}

    matched = 0
    missing = []
    for ident, qty, box, expiry in opis_items:
        item = by_article.get(ident) or by_barcode.get(ident)
        if not item:
            missing.append(ident)
            continue
        item.qty_picked = (item.qty_picked or 0) + qty
        if box and not item.box_label:
            item.box_label = box
        if expiry and not item.expiry_date:
            item.expiry_date = expiry
        matched += 1

    return {"matched": matched, "missing": missing}
