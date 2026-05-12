"""Сверка приходных и остатков."""
from dataclasses import dataclass
from typing import List

from sqlalchemy.orm import Session

from src.services.supply_service import get_supply


@dataclass
class Discrepancy:
    article: str
    expected: int
    actual: int
    delta: int  # actual - expected


def reconcile_prihod(session: Session, supply_id: int, prihod_items: List[tuple]) -> List[Discrepancy]:
    """prihod_items: List[(article, qty)].
    Сверяет с qty_planned по компонентам (НЕ по kit-строкам).
    """
    supply = get_supply(session, supply_id)
    if not supply:
        return []

    expected = {}
    for it in supply.items:
        # пропускаем kit-строки (у них есть expanded_from_kit_id у компонентов)
        # сам kit как строка имеет expanded_from_kit_id == None и одновременно есть компоненты
        # для упрощения — складываем по article всё, что НЕ помечено как expanded
        # → kit-строки и обычные одиночки
        # НО для компонентов kit'а используем компонент-строки.
        # Используем: если у sku есть компоненты в этой поставке (expanded_from_kit_id == sku.id у других), пропускаем сам kit
        is_kit_with_components = any(
            o.expanded_from_kit_id == it.sku_id for o in supply.items
        )
        if is_kit_with_components and it.expanded_from_kit_id is None:
            continue
        if not it.sku:
            continue
        expected[it.sku.article] = expected.get(it.sku.article, 0) + it.qty_planned

    actual_map = {}
    for article, qty in prihod_items:
        actual_map[article] = actual_map.get(article, 0) + qty

    discrepancies: List[Discrepancy] = []
    all_articles = set(expected) | set(actual_map)
    for art in sorted(all_articles):
        e = expected.get(art, 0)
        a = actual_map.get(art, 0)
        if e != a:
            discrepancies.append(Discrepancy(article=art, expected=e, actual=a, delta=a - e))

    return discrepancies
