"""Unit-тесты алгоритма выбора оптимальной даты bulk-бронирования.

См. план `C:\\Users\\vladi\\.claude\\plans\\smart-supply-booking.md` Фаза 3.
"""
from datetime import date

from src.services.auto_book import (
    SlotInfo,
    clusters_with_slots_on,
    date_options_summary,
    find_best_common_date,
    parse_timeslot_response,
    pick_earliest_slot,
)


def _slot(cluster: str, date_str: str, hour: int = 9, wh: int = 1) -> SlotInfo:
    return SlotInfo(
        cluster=cluster,
        warehouse_id=wh,
        warehouse_name=f"WH-{wh}",
        from_ts=f"{date_str}T{hour:02d}:00:00",
        to_ts=f"{date_str}T{hour + 1:02d}:00:00",
    )


def test_best_date_single_cluster_single_day():
    slots = {"Москва": [_slot("Москва", "2026-05-21")]}
    assert find_best_common_date(slots) == date(2026, 5, 21)


def test_best_date_all_clusters_same_day_wins():
    slots = {
        "Москва": [_slot("Москва", "2026-05-21"), _slot("Москва", "2026-05-22")],
        "СПб":    [_slot("СПб",    "2026-05-21")],
        "Самара": [_slot("Самара", "2026-05-21"), _slot("Самара", "2026-05-23")],
    }
    assert find_best_common_date(slots) == date(2026, 5, 21)


def test_best_date_partial_intersection():
    slots = {
        "Москва": [_slot("Москва", "2026-05-21"), _slot("Москва", "2026-05-22")],
        "СПб":    [_slot("СПб",    "2026-05-22"), _slot("СПб",    "2026-05-23")],
        "Самара": [_slot("Самара", "2026-05-22")],
        "Краснодар": [_slot("Краснодар", "2026-05-21")],
    }
    # 22.05 — 3 кластера (Москва+СПб+Самара), 21.05 — 2, 23.05 — 1
    assert find_best_common_date(slots) == date(2026, 5, 22)


def test_best_date_tie_breaker_picks_earlier():
    slots = {
        "A": [_slot("A", "2026-05-25"), _slot("A", "2026-05-30")],
        "B": [_slot("B", "2026-05-25"), _slot("B", "2026-05-30")],
    }
    # Обе даты дают 2 кластера — выбираем раньше
    assert find_best_common_date(slots) == date(2026, 5, 25)


def test_best_date_all_disjoint_picks_max_one_cluster_earliest():
    slots = {
        "A": [_slot("A", "2026-05-25")],
        "B": [_slot("B", "2026-05-26")],
        "C": [_slot("C", "2026-05-27")],
    }
    # Все даты дают по 1 кластеру — выбираем раньшую
    assert find_best_common_date(slots) == date(2026, 5, 25)


def test_best_date_no_slots():
    assert find_best_common_date({}) is None
    assert find_best_common_date({"A": [], "B": []}) is None


def test_best_date_respects_allowed_dates():
    slots = {
        "Москва": [_slot("Москва", "2026-05-21"), _slot("Москва", "2026-05-22")],
        "СПб":    [_slot("СПб",    "2026-05-21")],
    }
    # Юзер выбрал только 22.05 в /ship_plan
    result = find_best_common_date(slots, allowed_dates={date(2026, 5, 22)})
    assert result == date(2026, 5, 22)


def test_best_date_respects_allowed_hours():
    slots = {
        "Москва": [_slot("Москва", "2026-05-21", hour=9), _slot("Москва", "2026-05-22", hour=14)],
        "СПб":    [_slot("СПб",    "2026-05-21", hour=14)],
    }
    # Юзер указал часы 9-12 — слоты на 14:00 отфильтруются
    result = find_best_common_date(slots, allowed_hours={9, 10, 11, 12})
    # На 21.05 один кластер (Москва 9:00), 22.05 нет (Москва 14:00 не подходит)
    assert result == date(2026, 5, 21)


def test_clusters_with_slots_on_returns_dict():
    slots = {
        "Москва": [_slot("Москва", "2026-05-21"), _slot("Москва", "2026-05-22")],
        "СПб":    [_slot("СПб",    "2026-05-22")],
        "Самара": [_slot("Самара", "2026-05-23")],
    }
    out = clusters_with_slots_on(slots, date(2026, 5, 22))
    assert set(out.keys()) == {"Москва", "СПб"}
    assert len(out["Москва"]) == 1


def test_pick_earliest_slot():
    s1 = _slot("X", "2026-05-21", hour=14)
    s2 = _slot("X", "2026-05-21", hour=9)
    s3 = _slot("X", "2026-05-21", hour=11)
    assert pick_earliest_slot([s1, s2, s3]) == s2


def test_pick_earliest_slot_empty():
    assert pick_earliest_slot([]) is None


def test_date_options_summary_orders_correctly():
    slots = {
        "A": [_slot("A", "2026-05-21"), _slot("A", "2026-05-22")],
        "B": [_slot("B", "2026-05-22"), _slot("B", "2026-05-23")],
        "C": [_slot("C", "2026-05-22")],
    }
    summary = date_options_summary(slots)
    # 22.05 — 3, 21.05 и 23.05 по 1
    assert summary[0][0] == date(2026, 5, 22)
    assert summary[0][1] == 3
    assert summary[1][0] == date(2026, 5, 21)  # после тай-брейка по дате


def test_parse_timeslot_response_real_structure():
    """Структура взята из реального ответа Ozon (см. log Фазы 0)."""
    response = {
        "result": {
            "drop_off_warehouse_timeslots": {
                "days": [
                    {
                        "date_in_timezone": "2026-05-16",
                        "timeslots": [
                            {"from_in_timezone": "2026-05-16T12:00:00", "to_in_timezone": "2026-05-16T13:00:00"},
                            {"from_in_timezone": "2026-05-16T13:00:00", "to_in_timezone": "2026-05-16T14:00:00"},
                        ],
                    },
                    {
                        "date_in_timezone": "2026-05-17",
                        "timeslots": [
                            {"from_in_timezone": "2026-05-17T09:00:00", "to_in_timezone": "2026-05-17T10:00:00"},
                        ],
                    },
                ],
            }
        }
    }
    slots = parse_timeslot_response(response, cluster="Москва", warehouse_id=15431806189000, warehouse_name="ХОРУГВИНО_РФЦ")
    assert len(slots) == 3
    assert slots[0].date == date(2026, 5, 16)
    assert slots[0].hour == 12
    assert slots[2].date == date(2026, 5, 17)
    assert slots[2].hour == 9
    assert all(s.cluster == "Москва" for s in slots)


def test_parse_timeslot_response_empty():
    assert parse_timeslot_response({}, cluster="X", warehouse_id=0, warehouse_name="") == []
    assert parse_timeslot_response({"result": {}}, cluster="X", warehouse_id=0, warehouse_name="") == []
