"""Парсер 'Остатки Баковец *.xls' — остатки на складе ФФ.

Реальная структура (golden file: переписки/files/Остатки Баковец 06.05.xls):
  row 8: 'Код' | 'Артикул' | 'Наименование' | 'Ед.изм.' | 'Доступно' | 'Резерв' |
         'Ожидание' | 'Остаток' | 'Себестоимость' | 'Сумма себ.' | 'Цена прод' | 'Сумма прод' | 'Дней на складе'
  row 9: 'ИП Баковец Романович Владислав' (заголовок раздела)
  row 10+: данные
"""
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import xlrd


@dataclass
class OstatkiItem:
    article: str
    name: str
    available: int
    reserved: int
    pending: int
    balance: int
    days_at_warehouse: Optional[int]


def _norm(v) -> str:
    return str(v or "").strip().lower()


def parse_ostatki(path: str | Path) -> List[OstatkiItem]:
    book = xlrd.open_workbook(str(path))
    sheet = book.sheet_by_index(0)

    header_row = _find_header_row(sheet)
    if header_row is None:
        raise ValueError("ostatki: header row not found")

    headers = [_norm(sheet.cell_value(header_row, c)) for c in range(sheet.ncols)]
    idx = {}
    for i, h in enumerate(headers):
        if h == "артикул": idx["article"] = i
        elif "наименование" in h: idx["name"] = i
        elif "доступно" in h: idx["available"] = i
        elif "резерв" in h: idx["reserved"] = i
        elif "ожидание" in h: idx["pending"] = i
        elif h == "остаток": idx["balance"] = i
        elif "дней" in h and "склад" in h: idx["days"] = i

    if "article" not in idx or "balance" not in idx:
        raise ValueError(f"ostatki: missing required columns. Got: {headers}")

    items: List[OstatkiItem] = []
    for r in range(header_row + 1, sheet.nrows):
        article = sheet.cell_value(r, idx["article"])
        if not article or not str(article).strip():
            continue
        s = str(article).strip()
        if "ип " in s.lower() or s.lower().startswith("итого") or s.lower().startswith("всего"):
            continue

        items.append(OstatkiItem(
            article=s,
            name=str(sheet.cell_value(r, idx["name"])).strip() if "name" in idx else "",
            available=_to_int(sheet.cell_value(r, idx["available"])) if "available" in idx else 0,
            reserved=_to_int(sheet.cell_value(r, idx["reserved"])) if "reserved" in idx else 0,
            pending=_to_int(sheet.cell_value(r, idx["pending"])) if "pending" in idx else 0,
            balance=_to_int(sheet.cell_value(r, idx["balance"])),
            days_at_warehouse=_to_int(sheet.cell_value(r, idx["days"])) if "days" in idx else None,
        ))

    return items


def _find_header_row(sheet) -> Optional[int]:
    for r in range(min(20, sheet.nrows)):
        row = [_norm(sheet.cell_value(r, c)) for c in range(sheet.ncols)]
        if "артикул" in row and ("остаток" in row or "доступно" in row):
            return r
    return None


def _to_int(v) -> int:
    try:
        return int(float(v)) if v not in ("", None) else 0
    except (ValueError, TypeError):
        return 0
