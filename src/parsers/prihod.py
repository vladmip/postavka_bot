"""Парсер 'prihod-*.xls' — приходная от ФФ (старый бинарный xls).

Реальная структура (golden file: переписки/files/prihod-01152.xls):
  row 6: '№ п.п.' | 'Наименование' | ... | 'Ед. изм.' | 'Цена' | 'Кол-во' | 'Сумма'
  row 8+: данные. Артикул вшит в 'Наименование' до ' // '
    например 'HAIDILAO-GR // Лапша ... HaiDiLao 270г, гр'
"""
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import xlrd


@dataclass
class PrihodItem:
    article: str        # вычленяется из 'Наименование' до ' // '
    name: str           # полное Наименование
    qty: int            # 'Кол-во'
    unit: str           # 'Ед. изм.'
    price: float        # 'Цена'
    total: float        # 'Сумма'


@dataclass
class PrihodDocument:
    doc_number: Optional[str]
    items: List[PrihodItem]


def _norm(v) -> str:
    return str(v or "").strip().lower()


def parse_prihod(path: str | Path) -> PrihodDocument:
    book = xlrd.open_workbook(str(path))
    sheet = book.sheet_by_index(0)

    header_row = _find_header_row(sheet)
    if header_row is None:
        raise ValueError("prihod: header row not found")

    headers = [_norm(sheet.cell_value(header_row, c)) for c in range(sheet.ncols)]
    idx = {}
    for i, h in enumerate(headers):
        if "наименование" in h: idx["name"] = i
        elif "ед" in h and "изм" in h: idx["unit"] = i
        elif "цена" in h: idx["price"] = i
        elif "кол-во" in h or "количество" in h: idx["qty"] = i
        elif h == "сумма": idx["total"] = i

    if "name" not in idx or "qty" not in idx:
        raise ValueError(f"prihod: missing required columns. Got headers: {headers}")

    items: List[PrihodItem] = []
    for r in range(header_row + 1, sheet.nrows):
        name_raw = sheet.cell_value(r, idx["name"])
        if not name_raw or not str(name_raw).strip():
            continue
        s = str(name_raw).strip()
        if s.lower().startswith("итого") or s.lower().startswith("всего") or "сумма прописью" in s.lower():
            continue
        qty_v = sheet.cell_value(r, idx["qty"]) if "qty" in idx else 0
        try:
            qty = int(float(qty_v)) if qty_v not in ("", None) else 0
        except (ValueError, TypeError):
            continue
        if qty <= 0:
            continue

        article, _, _ = s.partition(" // ")
        article = article.strip() or s

        items.append(PrihodItem(
            article=article,
            name=s,
            qty=qty,
            unit=str(sheet.cell_value(r, idx["unit"])).strip() if "unit" in idx else "",
            price=_to_float(sheet.cell_value(r, idx["price"])) if "price" in idx else 0.0,
            total=_to_float(sheet.cell_value(r, idx["total"])) if "total" in idx else 0.0,
        ))

    doc_num = _extract_doc_number(sheet)
    return PrihodDocument(doc_number=doc_num, items=items)


def _find_header_row(sheet) -> Optional[int]:
    for r in range(min(20, sheet.nrows)):
        row = [_norm(sheet.cell_value(r, c)) for c in range(sheet.ncols)]
        joined = " | ".join(row)
        if "наименование" in joined and ("кол-во" in joined or "количество" in joined):
            return r
    return None


def _extract_doc_number(sheet) -> Optional[str]:
    for r in range(min(5, sheet.nrows)):
        for c in range(sheet.ncols):
            v = str(sheet.cell_value(r, c) or "")
            if "приходная" in v.lower():
                parts = v.split("№")
                if len(parts) > 1:
                    return parts[1].split()[0].strip()
    return None


def _to_float(v) -> float:
    try:
        return float(v) if v not in ("", None) else 0.0
    except (ValueError, TypeError):
        return 0.0
