"""CRUD каталога SKU + раскрытие наборов."""
from typing import List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import Sku, SkuKitLink


def get_sku(session: Session, sku_id: int) -> Optional[Sku]:
    return session.get(Sku, sku_id)


def find_sku_by_article(session: Session, article: str) -> Optional[Sku]:
    return session.scalar(select(Sku).where(Sku.article == article))


def find_sku_by_barcode(session: Session, barcode: str) -> Optional[Sku]:
    return session.scalar(select(Sku).where(Sku.barcode == barcode))


def list_skus(session: Session, limit: int = 50, offset: int = 0) -> List[Sku]:
    return list(session.scalars(
        select(Sku).order_by(Sku.article).limit(limit).offset(offset)
    ))


def upsert_sku(
    session: Session,
    barcode: str,
    article: str,
    name: str,
    intake_mode: str = "piece",
    intake_box_qty: Optional[int] = None,
    photo_required: bool = False,
    mark_required: bool = False,
) -> Tuple[Sku, bool]:
    """Возвращает (Sku, created?)."""
    existing = find_sku_by_barcode(session, barcode)
    if existing:
        return existing, False
    sku = Sku(
        barcode=barcode,
        article=article,
        name=name,
        intake_mode=intake_mode,
        intake_box_qty=intake_box_qty,
        photo_required=photo_required,
        mark_required=mark_required,
    )
    session.add(sku)
    session.flush()
    return sku, True


def add_kit_component(
    session: Session, kit_sku_id: int, component_sku_id: int, qty: int
) -> SkuKitLink:
    link = SkuKitLink(kit_sku_id=kit_sku_id, component_sku_id=component_sku_id, qty=qty)
    session.add(link)
    session.flush()
    return link


def get_kit_components(session: Session, kit_sku_id: int) -> List[SkuKitLink]:
    return list(session.scalars(
        select(SkuKitLink).where(SkuKitLink.kit_sku_id == kit_sku_id)
    ))


def expand_kit(session: Session, sku_id: int, qty: int) -> List[Tuple[Sku, int]]:
    """Если SKU — kit, возвращает компоненты с умноженным qty.
    Если не kit, возвращает [(sku, qty)].
    """
    components = get_kit_components(session, sku_id)
    if not components:
        sku = get_sku(session, sku_id)
        return [(sku, qty)]
    return [(c.component, c.qty * qty) for c in components]
