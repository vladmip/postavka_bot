"""Синхронизация статусов Ozon supply orders для заявок.

Источник: /v3/supply-order/get (см. ozon_api_docs.txt:13427+).
Маппинг state-enum → русские лейблы согласован с Ozon ЛК.

Кэш — in-memory по request_id с TTL 3 мин. После TTL автоматически зовём API
при следующем запросе (или принудительно через force=True). Кэш «мягкий»: его
задача — не дёргать API чаще раза в 3 мин при просмотре карточки.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from src.db.models import ShipmentItem, ShipmentRequest
from src.integrations.ozon_api import OzonClient

logger = logging.getLogger("services.ozon_supply_status")

_TTL_SEC = 180  # 3 минуты
_last_synced: Dict[int, float] = {}   # request_id → unix-ts


# Маппинг state → (emoji, label). Покрывает всё что отдаёт /v3/supply-order/get.
STATUS_DISPLAY: Dict[str, tuple[str, str]] = {
    "UNSPECIFIED": ("❔", "не указан"),
    "DATA_FILLING": ("📝", "Заполнение данных"),
    "READY_TO_SUPPLY": ("📦", "Готово к отгрузке"),
    "ACCEPTED_AT_SUPPLY_WAREHOUSE": ("🚚", "Принято на drop-off"),
    "IN_TRANSIT": ("🚛", "В пути на склад"),
    "ACCEPTANCE_AT_STORAGE_WAREHOUSE": ("📥", "Приёмка"),
    "REPORTS_CONFIRMATION_AWAITING": ("📄", "Согласование актов"),
    "REPORT_REJECTED": ("⚠", "Акты отклонены"),
    "COMPLETED": ("✅", "Завершено"),
    "REJECTED_AT_SUPPLY_WAREHOUSE": ("🚫", "Отклонено на drop-off"),
    "CANCELLED": ("❌", "Отменено"),
    "OVERDUE": ("⏰", "Просрочено"),
}

# Статусы, в которых ещё можно скачивать ТЗ для ФФ (товар ещё не уехал).
SCHEDULABLE_STATES = frozenset({"DATA_FILLING", "READY_TO_SUPPLY"})


@dataclass
class StatusInfo:
    state: str
    emoji: str
    label: str
    order_number: Optional[str]


def status_info(state: Optional[str], order_number: Optional[str] = None) -> StatusInfo:
    emoji, label = STATUS_DISPLAY.get((state or "").upper(), ("❔", state or "—"))
    return StatusInfo(state=state or "", emoji=emoji, label=label, order_number=order_number)


def is_cache_fresh(request_id: int) -> bool:
    last = _last_synced.get(request_id)
    return last is not None and (time.time() - last) < _TTL_SEC


def _invalidate(request_id: int) -> None:
    _last_synced.pop(request_id, None)


async def refresh_supply_status(
    session: Session,
    client: OzonClient,
    request_id: int,
    force: bool = False,
) -> int:
    """Обновить статусы всех Ozon-направлений заявки. Возвращает кол-во затронутых items.
    Если cache fresh и force=False — возвращает 0, ничего не делает."""
    if not force and is_cache_fresh(request_id):
        return 0

    req = session.get(ShipmentRequest, request_id)
    if not req:
        return 0

    order_ids: List[int] = []
    for it in req.items:
        if it.marketplace != "ozon" or not it.booked_supply_id:
            continue
        try:
            order_ids.append(int(it.booked_supply_id))
        except (TypeError, ValueError):
            continue
    if not order_ids:
        return 0

    try:
        orders = await client.supply_order_get(order_ids)
    except Exception as e:
        logger.exception("supply_order_get failed for rid=%s: %s", request_id, e)
        raise

    by_id: Dict[int, dict] = {}
    for o in orders:
        try:
            by_id[int(o.get("order_id"))] = o
        except (TypeError, ValueError):
            continue

    affected = 0
    now = datetime.utcnow()
    for it in req.items:
        if it.marketplace != "ozon" or not it.booked_supply_id:
            continue
        try:
            key = int(it.booked_supply_id)
        except (TypeError, ValueError):
            continue
        o = by_id.get(key)
        if not o:
            continue
        new_state = (o.get("state") or "").strip().upper()
        order_num = o.get("order_number")
        dropoff = (o.get("dropoff_warehouse") or {}).get("name")

        # CANCELLED → откатываем item в «незабронированное» состояние, чтобы
        # юзер мог пробронировать заново. Историю не теряем — Ozon ЛК хранит.
        if new_state == "CANCELLED":
            it.booked_supply_id = None
            it.booked_slot_at = None
            it.target_warehouse = None
            it.ozon_supply_status = None
            it.ozon_supply_status_at = None
            it.ozon_order_number = None
            it.ozon_dropoff_name = None
            affected += 1
            continue

        changed = (
            it.ozon_supply_status != new_state
            or it.ozon_order_number != order_num
            or it.ozon_dropoff_name != dropoff
        )
        if changed or it.ozon_supply_status_at is None:
            it.ozon_supply_status = new_state
            it.ozon_supply_status_at = now
            if order_num:
                it.ozon_order_number = str(order_num)
            if dropoff:
                it.ozon_dropoff_name = dropoff
            affected += 1

    # Если cancelled был — пересчитать state заявки (мог откатиться в planning).
    from src.services.shipment_service import refresh_request_state_after_booking
    refresh_request_state_after_booking(req)

    _last_synced[request_id] = time.time()
    session.flush()
    return affected


async def cancel_supply_orders(
    session: Session,
    client: OzonClient,
    order_ids: List[int],
    poll_attempts: int = 5,
    poll_interval_sec: int = 3,
) -> List[Dict[str, object]]:
    """Массовая отмена supply orders. Для каждого:
    1) Дёргает /v1/supply-order/cancel → operation_id.
    2) Поллит /v1/supply-order/cancel/status пока не получим is_order_cancelled.
    Возвращает список {order_id, cancelled: bool, error: str|None}."""
    results: List[Dict[str, object]] = []
    for oid in order_ids:
        try:
            op_id = await client.supply_order_cancel(oid)
        except Exception as e:
            logger.exception("supply_order_cancel(%s) failed", oid)
            results.append({"order_id": oid, "cancelled": False, "error": str(e)[:120]})
            continue
        if not op_id:
            results.append({"order_id": oid, "cancelled": False, "error": "no operation_id"})
            continue
        cancelled = False
        last_err = None
        for _ in range(poll_attempts):
            await asyncio.sleep(poll_interval_sec)
            try:
                st = await client.supply_order_cancel_status(op_id)
            except Exception as e:
                last_err = str(e)[:120]
                continue
            errs = st.get("error_reasons") or []
            res = st.get("result") or {}
            if errs:
                last_err = ", ".join(errs)
                break
            if res.get("is_order_cancelled"):
                cancelled = True
                break
        results.append({"order_id": oid, "cancelled": cancelled, "error": last_err})
    return results
