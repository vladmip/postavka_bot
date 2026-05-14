"""Чистка каталога SKU от мусора, который попадает из xlsx-парсера.

Типичные мусорные строки: служебные команды, голые штрихкоды без артикула,
фрагменты дат/хабов, стоп-слова. Конкретные критерии — см. _trash_flags.
"""
from __future__ import annotations

import logging
from typing import Dict, List

from sqlalchemy.orm import Session

from src.db.models import Sku

logger = logging.getLogger("services.catalog_cleanup")

# Точные совпадения (lower-case). Это явные «не-артикулы».
_STOP_WORDS = {
    "озон", "ozon", "вб", "wb", "wildberries", "wildberris", "валдбериз",
    "коробки", "коробка", "поставка", "приёмка", "приемка",
    "название", "название товара", "артикул", "карта", "карты",
    "ширина", "длина", "высота", "вес", "товар",
}


def _trash_flags(sku: Sku) -> List[str]:
    """Список причин почему этот SKU выглядит мусорным. Пустой = норм."""
    art = (sku.article or "").strip()
    art_low = art.lower()
    flags: List[str] = []
    if not art:
        flags.append("EMPTY")
        return flags
    if art.startswith("/"):
        flags.append("CMD")  # /startt, /supply_delete и т.п.
    if art_low in _STOP_WORDS:
        flags.append("STOP_WORD")
    if len(art) <= 2:
        flags.append("TOO_SHORT")
    # Голый штрихкод (digits 12+) в поле артикула — парсер взял barcode-колонку
    if art.isdigit() and len(art) >= 12:
        flags.append("BARE_BARCODE")
    # Содержит метку хаба/кросс-дока/возврата — точно фрагмент описания
    art_up = art.upper()
    for marker in ("_ХАБ", "_XD", "_РФЦ", "_ВОЗВРАТЫ", "_КГТ", "ЩЕРБИНКА",
                   "ХОРУГВИНО", "ПУШКИНО", "ДОМОДЕДОВО"):
        if marker in art_up:
            # Эти слова в артикуле — индикатор что строка скопирована
            # из «адрес/название склада», а не артикула товара
            flags.append("WAREHOUSE_NAME")
            break
    return flags


def find_trash(session: Session) -> List[Dict]:
    """Найти все SKU с мусорными признаками. Возвращает [{id, article, flags}]."""
    rows = session.query(Sku).all()
    trash: List[Dict] = []
    for s in rows:
        flags = _trash_flags(s)
        if flags:
            trash.append({
                "id": s.id,
                "article": s.article,
                "barcode": s.barcode,
                "flags": flags,
            })
    return trash


def delete_skus(session: Session, ids: List[int]) -> int:
    """Удалить SKU по списку ID. Возвращает число удалённых.

    ⚠ Удаление каскадно зацепит kit-links и привязки в shipment_items.
    Перед вызовом убедиться что эти SKU не используются в активных поставках.
    """
    if not ids:
        return 0
    deleted = 0
    for sid in ids:
        row = session.get(Sku, sid)
        if row:
            session.delete(row)
            deleted += 1
    return deleted
