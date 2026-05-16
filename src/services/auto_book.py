"""Алгоритм поиска оптимальной даты для bulk-бронирования Ozon-поставки.

Идея (см. C:\\Users\\vladi\\.claude\\plans\\smart-supply-booking.md):
  - У юзера N кластеров и список разрешённых дат (target_dates_json).
  - Для каждого кластера дёрнули `/v1/draft/timeslot/info` → набор {date: [slots]}.
  - Хотим найти дату, на которую согласно максимально много кластеров одновременно.
  - Tie-break: чем раньше — тем лучше (быстрее отгрузить).

Эту функцию используют:
  - `ozon_book._auto_booking_pipeline` (новый flow).
  - Тесты `tests/test_auto_book.py`.

Headless / без зависимостей от aiogram — чтобы можно было unit-тестить.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date as _date, datetime as _dt
from typing import Dict, Iterable, List, Optional, Set


@dataclass(frozen=True)
class SlotInfo:
    """Один таймслот в результате `draft_timeslot_info`."""
    cluster: str
    warehouse_id: int
    warehouse_name: str
    from_ts: str        # "2026-05-21T09:00:00" (ISO без zone — Ozon отдаёт в локальной TZ)
    to_ts: str

    @property
    def date(self) -> _date:
        return _parse_date(self.from_ts)

    @property
    def hour(self) -> int:
        try:
            return int(self.from_ts[11:13])
        except (ValueError, IndexError):
            return 0


def _parse_date(iso: str) -> _date:
    """Парсит '2026-05-21T09:00:00' → date(2026,5,21). Не падает на мусоре."""
    try:
        return _dt.fromisoformat(iso[:19]).date()
    except (ValueError, TypeError):
        return _date(1970, 1, 1)


def parse_timeslot_response(
    response: Dict,
    *,
    cluster: str,
    warehouse_id: int,
    warehouse_name: str,
) -> List[SlotInfo]:
    """Парсит ответ `/v1/draft/timeslot/info` в плоский список SlotInfo.

    Структура ответа:
      { "result": { "drop_off_warehouse_timeslots": {
            "days": [
              { "date_in_timezone": "2026-05-21",
                "timeslots": [{"from_in_timezone": "...", "to_in_timezone": "..."}, ...] },
              ...
            ]
      } } }
    Также бывает `warehouse_timeslots` (DIRECT) или ещё что — берём все.
    """
    out: List[SlotInfo] = []
    result = response.get("result") or response
    # Ozon в разных endpoint'ах кладёт под разные ключи. Берём все варианты.
    containers = []
    for key in (
        "drop_off_warehouse_timeslots",
        "warehouse_timeslots",
        "warehouses_timeslots",
    ):
        v = result.get(key)
        if isinstance(v, dict):
            containers.append(v)
        elif isinstance(v, list):
            containers.extend(c for c in v if isinstance(c, dict))
    if not containers:
        # fallback: бывает прямо days[] в корне
        if "days" in result:
            containers.append(result)
    for c in containers:
        for day in (c.get("days") or []):
            for ts in (day.get("timeslots") or []):
                from_ts = str(ts.get("from_in_timezone") or "")
                to_ts = str(ts.get("to_in_timezone") or "")
                if from_ts and to_ts:
                    out.append(SlotInfo(
                        cluster=cluster,
                        warehouse_id=warehouse_id,
                        warehouse_name=warehouse_name,
                        from_ts=from_ts,
                        to_ts=to_ts,
                    ))
    return out


def find_best_common_date(
    slots_per_cluster: Dict[str, List[SlotInfo]],
    *,
    allowed_dates: Optional[Set[_date]] = None,
    allowed_hours: Optional[Set[int]] = None,
) -> Optional[_date]:
    """Выбирает дату, на которой максимальное число кластеров может уехать.

    - `slots_per_cluster`: {cluster_name: [SlotInfo,...]}.
    - `allowed_dates`: если задано, рассматриваем только эти даты
      (это даты которые юзер пометил в /ship_plan). Если None — все.
    - `allowed_hours`: фильтр по часам старта слота (юзер в hp picker'е).
      Если None — все часы.

    Возвращает date | None (None если нет общей даты ни для одного кластера).

    Tie-break: если на 2 даты одинаковое число кластеров — берём ближайшую.
    """
    by_date: Dict[_date, Set[str]] = {}
    for cluster, slots in slots_per_cluster.items():
        for s in slots:
            d = s.date
            if allowed_dates is not None and d not in allowed_dates:
                continue
            if allowed_hours is not None and s.hour not in allowed_hours:
                continue
            by_date.setdefault(d, set()).add(cluster)
    if not by_date:
        return None
    today = _date.today()
    return max(
        by_date.items(),
        key=lambda kv: (len(kv[1]), -((kv[0] - today).days)),
    )[0]


def clusters_with_slots_on(
    slots_per_cluster: Dict[str, List[SlotInfo]],
    target_date: _date,
    *,
    allowed_hours: Optional[Set[int]] = None,
) -> Dict[str, List[SlotInfo]]:
    """Для каждого кластера — список слотов **на заданную дату** (с учётом
    фильтра по часам). Кластеры без слотов в этот день не попадают в результат."""
    out: Dict[str, List[SlotInfo]] = {}
    for cluster, slots in slots_per_cluster.items():
        chosen = [
            s for s in slots
            if s.date == target_date
            and (allowed_hours is None or s.hour in allowed_hours)
        ]
        if chosen:
            out[cluster] = chosen
    return out


def pick_earliest_slot(slots: Iterable[SlotInfo]) -> Optional[SlotInfo]:
    """Самый ранний слот по времени (для bulk-book выбираем «утренний»)."""
    sl = sorted(slots, key=lambda s: s.from_ts)
    return sl[0] if sl else None


def date_options_summary(
    slots_per_cluster: Dict[str, List[SlotInfo]],
    *,
    allowed_dates: Optional[Set[_date]] = None,
    allowed_hours: Optional[Set[int]] = None,
) -> List[tuple]:
    """Для UI: список (date, cluster_count, list_of_clusters), отсортирован
    по убыванию числа кластеров, потом по дате asc. Топ-N покажем юзеру."""
    by_date: Dict[_date, Set[str]] = {}
    for cluster, slots in slots_per_cluster.items():
        for s in slots:
            d = s.date
            if allowed_dates is not None and d not in allowed_dates:
                continue
            if allowed_hours is not None and s.hour not in allowed_hours:
                continue
            by_date.setdefault(d, set()).add(cluster)
    items = [(d, len(cs), sorted(cs)) for d, cs in by_date.items()]
    items.sort(key=lambda x: (-x[1], x[0]))
    return items
