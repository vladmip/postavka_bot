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
    drop_off_warehouse_name: Optional[str] = None,
) -> None:
    """Сохранить только что созданный draft в кэш."""
    row = OzonDraftCache(
        request_id=request_id,
        cluster=cluster,
        cluster_id=cluster_id,
        draft_id=draft_id,
        supply_type=supply_type,
        drop_off_warehouse_id=drop_off_warehouse_id,
        drop_off_warehouse_name=drop_off_warehouse_name,
    )
    session.add(row)


def get_dropoff_choices_for_request(
    session: Session, request_id: int,
) -> dict:
    """Восстановить { cluster_name: {wh_id, name} } из cached drafts заявки.

    Нужно для повторного входа в CROSSDOCK-флоу — чтобы не спрашивать
    drop-off-точку заново, если она уже была выбрана и сохранена в draft."""
    from datetime import datetime, timedelta
    cutoff = datetime.utcnow() - timedelta(minutes=DRAFT_TTL_MIN)
    rows = (
        session.query(OzonDraftCache)
        .filter(
            OzonDraftCache.request_id == request_id,
            OzonDraftCache.created_at >= cutoff,
            OzonDraftCache.drop_off_warehouse_id.isnot(None),
        )
        .all()
    )
    return {
        r.cluster: {
            "wh_id": r.drop_off_warehouse_id,
            "name": r.drop_off_warehouse_name or f"#{r.drop_off_warehouse_id}",
        }
        for r in rows
    }


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
