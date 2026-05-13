"""Кэш созданных Ozon-драфтов: переиспользуем в окне 25 мин, чтобы не палить
лимит 2/мин на /v1/draft/*/create."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from src.db.models import OzonDraftCache

DRAFT_TTL_MIN = 25  # Ozon держит draft 30 мин — берём с запасом 5 мин


def _fresh_cutoff() -> datetime:
    return datetime.utcnow() - timedelta(minutes=DRAFT_TTL_MIN)


def get_fresh_draft(
    session: Session, request_id: int, cluster: str,
) -> Optional[Dict]:
    """Вернёт самый свежий неиспользованный draft для (request, cluster) или None."""
    row = (
        session.query(OzonDraftCache)
        .filter(
            OzonDraftCache.request_id == request_id,
            OzonDraftCache.cluster == cluster,
            OzonDraftCache.created_at >= _fresh_cutoff(),
            OzonDraftCache.used_at.is_(None),
        )
        .order_by(OzonDraftCache.created_at.desc())
        .first()
    )
    if not row:
        return None
    return {
        "id": row.id,
        "cluster": row.cluster,
        "cluster_id": row.cluster_id,
        "draft_id": row.draft_id,
        "supply_type": row.supply_type,
        "drop_off_warehouse_id": row.drop_off_warehouse_id,
        "age_sec": int((datetime.utcnow() - row.created_at).total_seconds()),
    }


def save_draft(
    session: Session,
    request_id: int,
    cluster: str,
    cluster_id: int,
    draft_id: int,
    supply_type: int,
    drop_off_warehouse_id: Optional[int] = None,
) -> None:
    """Сохранить только что созданный draft в кэш."""
    row = OzonDraftCache(
        request_id=request_id,
        cluster=cluster,
        cluster_id=cluster_id,
        draft_id=draft_id,
        supply_type=supply_type,
        drop_off_warehouse_id=drop_off_warehouse_id,
    )
    session.add(row)


def mark_draft_used(session: Session, draft_id: int) -> None:
    """Отметить draft как использованный (после успешной брони)."""
    row = (
        session.query(OzonDraftCache)
        .filter(OzonDraftCache.draft_id == draft_id)
        .first()
    )
    if row:
        row.used_at = datetime.utcnow()


def cleanup_expired(session: Session) -> int:
    """Удалить просроченные драфты (>30 мин). Возвращает число удалённых."""
    cutoff = datetime.utcnow() - timedelta(minutes=30)
    rows = session.query(OzonDraftCache).filter(OzonDraftCache.created_at < cutoff).all()
    n = len(rows)
    for r in rows:
        session.delete(r)
    return n
