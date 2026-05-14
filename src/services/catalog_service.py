"""CRUD каталога SKU + раскрытие наборов."""
from typing import List, Optional, Tuple

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from src.db.models import Sku, SkuKitLink


# Мапа кириллических букв → визуально идентичные латинские. Применяется ТОЛЬКО
# при сравнении (matching), не для сохранения в БД. Так бот устойчив к ошибкам
# раскладки (когда у продавца в карточке Ozon offer_id напечатан с русской «С»
# вместо латинской «C» — реальный кейс с товаром 3CHOC у Баковца).
_CYR_TO_LAT = str.maketrans({
    "А": "A", "В": "B", "С": "C", "Е": "E", "Н": "H", "К": "K", "М": "M",
    "О": "O", "Р": "P", "Т": "T", "Х": "X", "У": "Y", "І": "I", "Ј": "J",
    # Lowercase
    "а": "a", "в": "b", "с": "c", "е": "e", "н": "h", "к": "k", "м": "m",
    "о": "o", "р": "p", "т": "t", "х": "x", "у": "y", "і": "i", "ј": "j",
})


def normalize_for_match(s: Optional[str]) -> str:
    """Привести строку к canonical-форме для сравнения артикулов:
    - lower-case
    - кириллические двойники → латинские
    - strip whitespace
    Не меняет данные в БД; используется ТОЛЬКО как ключ при поиске."""
    if not s:
        return ""
    return s.strip().translate(_CYR_TO_LAT).lower()


def get_sku(session: Session, sku_id: int) -> Optional[Sku]:
    return session.get(Sku, sku_id)


def find_sku_by_article(session: Session, article: str) -> Optional[Sku]:
    """Поиск SKU по артикулу с нормализацией (lower + cyrillic→latin).
    Точное совпадение пробуем первым, потом fuzzy. Это защищает от
    «3CHOС» (рус. С) vs «3CHOC» (лат. C) и подобного."""
    if not article:
        return None
    exact = session.scalar(select(Sku).where(Sku.article == article))
    if exact:
        return exact
    target = normalize_for_match(article)
    if not target:
        return None
    for s in session.scalars(select(Sku)):
        if normalize_for_match(s.article) == target:
            return s
    return None


def find_sku_by_barcode(session: Session, barcode: str) -> Optional[Sku]:
    """Поиск по штрихкоду. Сначала точное, потом нормализованное.
    Нормализация безопасна — реальные штрихкоды это цифры (ничего не меняется),
    но защищает от тех же кириллица/латиница в строковых «псевдо-баркодах» (3CHOC)."""
    if not barcode:
        return None
    exact = session.scalar(select(Sku).where(Sku.barcode == barcode))
    if exact:
        return exact
    target = normalize_for_match(barcode)
    if not target:
        return None
    for s in session.scalars(select(Sku)):
        if normalize_for_match(s.barcode) == target:
            return s
    return None


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
