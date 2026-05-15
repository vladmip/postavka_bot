"""Сервис ProductHint: статистика каталога, резолв артикулов в product_id,
upsert / replace upload-ов с xlsx.

Привязка hint'а — по ozon_products.id (PK), чтобы пережить смену offer_id
продавцом. Юзер заливает по offer_id (что у него в xlsx), резолвим один раз.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.db.models import OzonProduct, ProductHint
from src.parsers.product_hints import HintRow
from src.services.shipment_service import _find_ozon_product

logger = logging.getLogger("services.product_hint")


@dataclass
class CatalogStats:
    total: int          # всего товаров в ozon_products
    with_hint: int      # сколько из них имеют ProductHint
    without_hint: int   # total - with_hint


@dataclass
class ResolvedRow:
    """Одна строка из xlsx, привязанная к товару каталога."""
    ozon_product_id: int
    offer_id: str        # фактический offer_id из каталога (после резолва)
    raw_article: str     # как было в xlsx
    packaging: Optional[str]
    notes: Optional[str]


@dataclass
class ResolveReport:
    matched: List[ResolvedRow]
    unmatched_articles: List[str]  # артикулы, которых нет в каталоге


def _resolve_user_id(user_id: Optional[int]) -> int:
    """Backward-compat fallback на ALLOWED_USER_ID."""
    if user_id is not None:
        return user_id
    from src.config import ALLOWED_USER_ID
    return ALLOWED_USER_ID


def _owned_product_filter(query, user_id: int):
    """Фильтр: только OzonProduct'ы юзера (или legacy NULL для ALLOWED_USER_ID)."""
    from src.config import ALLOWED_USER_ID
    if user_id == ALLOWED_USER_ID:
        return query.filter((OzonProduct.user_id == user_id) | (OzonProduct.user_id.is_(None)))
    return query.filter(OzonProduct.user_id == user_id)


def get_catalog_stats(session: Session, user_id: Optional[int] = None) -> CatalogStats:
    """Статистика каталога ТОЛЬКО для user_id."""
    uid = _resolve_user_id(user_id)
    total = _owned_product_filter(session.query(OzonProduct), uid).count() or 0
    # ProductHint без user_id — JOIN на ozon_products чтобы посчитать только свои.
    with_hint_q = (
        session.query(ProductHint)
        .join(OzonProduct, ProductHint.ozon_product_id == OzonProduct.id)
    )
    with_hint = _owned_product_filter(with_hint_q, uid).count() or 0
    return CatalogStats(total=total, with_hint=with_hint, without_hint=total - with_hint)


def resolve_rows(session: Session, rows: Iterable[HintRow], user_id: Optional[int] = None) -> ResolveReport:
    uid = _resolve_user_id(user_id)
    matched: List[ResolvedRow] = []
    unmatched: List[str] = []
    seen_pids: set = set()
    for r in rows:
        product = _find_ozon_product(session, r.article, uid)
        if product is None:
            unmatched.append(r.article)
            continue
        # Если в xlsx несколько строк ссылаются на один и тот же товар
        # (например через barcode-fallback) — последняя выигрывает.
        if product.id in seen_pids:
            # обновляем уже добавленную ResolvedRow
            for m in matched:
                if m.ozon_product_id == product.id:
                    m.packaging = r.packaging
                    m.notes = r.notes
                    m.raw_article = r.article
                    break
            continue
        seen_pids.add(product.id)
        matched.append(ResolvedRow(
            ozon_product_id=product.id,
            offer_id=product.offer_id,
            raw_article=r.article,
            packaging=r.packaging,
            notes=r.notes,
        ))
    return ResolveReport(matched=matched, unmatched_articles=unmatched)


def _owned_product_ids(session: Session, user_id: int) -> set[int]:
    """ID всех OzonProduct'ов юзера (для фильтрации hint'ов через JOIN-замену)."""
    rows = _owned_product_filter(session.query(OzonProduct.id), user_id).all()
    return {r[0] for r in rows}


def apply_upsert(session: Session, rows: Iterable[ResolvedRow], user_id: Optional[int] = None) -> int:
    """Обновляем только те product_id, которые в файле И принадлежат user_id.
    Чужие игнорируем (защита от подсунутого ozon_product_id)."""
    uid = _resolve_user_id(user_id)
    owned = _owned_product_ids(session, uid)
    count = 0
    for r in rows:
        if r.ozon_product_id not in owned:
            continue
        existing = session.scalar(
            select(ProductHint).where(ProductHint.ozon_product_id == r.ozon_product_id)
        )
        if existing:
            existing.packaging = r.packaging
            existing.notes = r.notes
        else:
            session.add(ProductHint(
                ozon_product_id=r.ozon_product_id,
                packaging=r.packaging,
                notes=r.notes,
            ))
        count += 1
    session.flush()
    return count


def apply_replace(session: Session, rows: Iterable[ResolvedRow], user_id: Optional[int] = None) -> Tuple[int, int]:
    """Чистим product_hints ТОЛЬКО для товаров user_id (не трогаем чужие)
    и записываем те ozon_product_id, что принадлежат user_id."""
    uid = _resolve_user_id(user_id)
    owned = _owned_product_ids(session, uid)
    if owned:
        deleted = session.query(ProductHint).filter(
            ProductHint.ozon_product_id.in_(owned)
        ).delete(synchronize_session=False)
    else:
        deleted = 0
    inserted = 0
    for r in rows:
        if r.ozon_product_id not in owned:
            continue
        session.add(ProductHint(
            ozon_product_id=r.ozon_product_id,
            packaging=r.packaging,
            notes=r.notes,
        ))
        inserted += 1
    session.flush()
    return deleted, inserted


def get_hints_by_product_ids(
    session: Session, product_ids: Iterable[int],
) -> dict[int, ProductHint]:
    """Берёт hints по списку product_ids. Caller обязан сам убедиться, что
    все ID принадлежат текущему юзеру (либо вызывать только из контекста
    уже отфильтрованной заявки/каталога). Не делаем доп. JOIN ради простоты."""
    ids = list(product_ids)
    if not ids:
        return {}
    rows = session.scalars(
        select(ProductHint).where(ProductHint.ozon_product_id.in_(ids))
    ).all()
    return {h.ozon_product_id: h for h in rows}
