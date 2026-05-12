"""Генератор ТЗ Отгрузка xlsx для ShipmentRequest.

Структура шаблона `otgruzka_template_v2.xlsx`:
  Лист 'вб':
    R1: 'ШК' | 'Название' | 'Поставщик' | <склад_1> | <склад_2> | 'упаковка' | 'состав набора' | 'Артикул'
    R2: <empty> | <empty> | <empty> | <supply_id> | <supply_id> | ...
    R3: <empty> | <empty> | <empty> | <date> | <date> | ...
    R4+: данные (одна строка на SKU)
  Лист 'озон':
    R1: 'ШК' | 'Название' | 'Поставщик' | <кластер_1> | ... | 'Упаковка' | 'примечание' | 'Артикул' | 'Полный адрес+таймслот'
    R2+: данные

Мы создаём колонки по фактическим направлениям заявки. Колонки supply_id и date
заполняются позже (после бронирования через API), пока пусто.
"""
from __future__ import annotations

import logging
from io import BytesIO
from pathlib import Path
from typing import Dict, List, Tuple

from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill, Border, Side

from src.db.models import ShipmentRequest, ShipmentItem

logger = logging.getLogger("generators.ship_tz")


TEMPLATE_PATH = Path(__file__).parent / "templates" / "otgruzka_template_v2.xlsx"


def _styled_header_fill():
    return PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")


def _thin_border():
    side = Side(style="thin", color="999999")
    return Border(left=side, right=side, top=side, bottom=side)


def generate_ship_tz(req: ShipmentRequest) -> bytes:
    """Сгенерировать ТЗ Отгрузка xlsx для заявки. Возвращает bytes."""
    # Грузим items в простые структуры (req должен быть доступен внутри сессии)
    by_mp_cluster_sku: Dict[Tuple[str, str, int], Dict] = {}
    skus_meta: Dict[int, Dict] = {}  # sku_id → {barcode, name, article, kit_components}
    unmatched_lines: List[Dict] = []  # позиции без SKU

    for it in req.items:
        if it.sku is None:
            unmatched_lines.append({
                "marketplace": it.marketplace,
                "cluster": it.cluster,
                "raw_article": it.raw_article,
                "qty": it.qty,
            })
            continue
        sku = it.sku
        key = (it.marketplace, it.cluster, sku.id)
        prev = by_mp_cluster_sku.get(key, {"qty": 0})
        by_mp_cluster_sku[key] = {"qty": prev["qty"] + it.qty}
        if sku.id not in skus_meta:
            skus_meta[sku.id] = {
                "barcode": sku.barcode,
                "name": sku.name,
                "article": sku.article,
            }

    # Уникальные кластеры по МП (сохраняем порядок появления)
    wb_clusters: List[str] = []
    oz_clusters: List[str] = []
    for (mp, cl, _sku_id) in by_mp_cluster_sku.keys():
        target = wb_clusters if mp == "wb" else oz_clusters
        if cl not in target:
            target.append(cl)

    # Уникальные SKU по МП
    def skus_for(mp: str) -> List[int]:
        seen = []
        for (m, _cl, sku_id) in by_mp_cluster_sku.keys():
            if m == mp and sku_id not in seen:
                seen.append(sku_id)
        return seen

    wb_skus = skus_for("wb")
    oz_skus = skus_for("oz") + skus_for("ozon")  # на всякий случай
    # Уникальные
    oz_skus = list(dict.fromkeys(skus_for("ozon")))

    # Открываем шаблон или создаём с нуля если шаблон сломан
    try:
        wb = load_workbook(TEMPLATE_PATH)
    except Exception as e:
        logger.warning("Template load failed (%s) — creating from scratch", e)
        from openpyxl import Workbook
        wb = Workbook()
        wb.remove(wb.active)
        wb.create_sheet("вб")
        wb.create_sheet("озон")

    # Удалим всё существующее содержимое (но сохраним структуру листов)
    for sheet_name in list(wb.sheetnames):
        if sheet_name in ("вб", "озон"):
            ws = wb[sheet_name]
            ws.delete_rows(1, ws.max_row + 1)

    # ── Лист 'вб' ───────────────────────────────────────────────────────────
    if wb_skus:
        if "вб" not in wb.sheetnames:
            wb.create_sheet("вб")
        ws = wb["вб"]
        _fill_sheet_wb(ws, req, wb_clusters, wb_skus, skus_meta, by_mp_cluster_sku)
    else:
        if "вб" in wb.sheetnames:
            wb.remove(wb["вб"])

    # ── Лист 'озон' ─────────────────────────────────────────────────────────
    if oz_skus:
        if "озон" not in wb.sheetnames:
            wb.create_sheet("озон")
        ws = wb["озон"]
        _fill_sheet_ozon(ws, req, oz_clusters, oz_skus, skus_meta, by_mp_cluster_sku)
    else:
        if "озон" in wb.sheetnames:
            wb.remove(wb["озон"])

    # ── Лист 'операции' с unmatched и сводкой ───────────────────────────────
    if "операции" in wb.sheetnames:
        wb.remove(wb["операции"])
    ws_op = wb.create_sheet("операции")
    ws_op.append(["Заявка", f"#{req.id}"])
    ws_op.append(["Создана", req.created_at.strftime("%Y-%m-%d %H:%M")])
    if req.target_date_from:
        date_s = req.target_date_from.strftime("%Y-%m-%d")
        if req.target_date_to:
            date_s += f" — {req.target_date_to:%Y-%m-%d}"
        ws_op.append(["Целевые даты", date_s])
    ws_op.append([])
    if unmatched_lines:
        ws_op.append(["⚠ Позиции без SKU в каталоге:"])
        ws_op.append(["МП", "Кластер", "Артикул", "Кол-во"])
        for u in unmatched_lines:
            ws_op.append([u["marketplace"].upper(), u["cluster"], u["raw_article"], u["qty"]])

    out = BytesIO()
    wb.save(out)
    return out.getvalue()


def _fill_sheet_wb(ws, req: ShipmentRequest, clusters: List[str], sku_ids: List[int],
                   skus_meta: Dict, by_key: Dict) -> None:
    """Лист 'вб' — структура из шаблона."""
    header_fill = _styled_header_fill()
    border = _thin_border()
    bold = Font(bold=True)

    # Заголовок
    fixed_left = ["ШК", "Название товара", "Поставщик"]
    fixed_right = ["упаковка", "состав набора", "Артикул Поставщика"]
    headers = fixed_left + clusters + fixed_right

    for col_idx, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_idx, value=h)
        cell.font = bold
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = border

    # R2 = supply_id (заполняется после бронирования)
    # R3 = дата (заполняется после бронирования)
    for col_idx in range(len(fixed_left) + 1, len(fixed_left) + len(clusters) + 1):
        ws.cell(row=2, column=col_idx, value="").border = border
        ws.cell(row=3, column=col_idx, value="").border = border

    # Данные с R4
    row_idx = 4
    for sku_id in sku_ids:
        meta = skus_meta[sku_id]
        ws.cell(row=row_idx, column=1, value=meta["barcode"])
        ws.cell(row=row_idx, column=2, value=meta["name"])
        ws.cell(row=row_idx, column=3, value="ИП Баковец")
        # qty по кластерам
        for c_idx, cluster in enumerate(clusters):
            col = len(fixed_left) + 1 + c_idx
            entry = by_key.get(("wb", cluster, sku_id))
            if entry:
                ws.cell(row=row_idx, column=col, value=entry["qty"])
        # справа: артикул в последнюю колонку
        right_start = len(fixed_left) + len(clusters) + 1
        ws.cell(row=row_idx, column=right_start, value=None)   # упаковка — оставляем для ручного
        ws.cell(row=row_idx, column=right_start + 1, value=None)   # состав
        ws.cell(row=row_idx, column=right_start + 2, value=meta["article"])
        for c in range(1, len(headers) + 1):
            ws.cell(row=row_idx, column=c).border = border
        row_idx += 1

    # Ширина колонок
    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 36
    ws.column_dimensions["C"].width = 14


def _fill_sheet_ozon(ws, req: ShipmentRequest, clusters: List[str], sku_ids: List[int],
                     skus_meta: Dict, by_key: Dict) -> None:
    """Лист 'озон' — структура из шаблона."""
    header_fill = _styled_header_fill()
    border = _thin_border()
    bold = Font(bold=True)

    fixed_left = ["ШК", "Название товара", "Поставщик"]
    fixed_right = [
        "Упаковка для товара", "примечание",
        "Артикул Поставщика", "Полный адрес склада и таймслот",
    ]
    headers = fixed_left + clusters + fixed_right

    for col_idx, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_idx, value=h)
        cell.font = bold
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = border

    row_idx = 2
    for sku_id in sku_ids:
        meta = skus_meta[sku_id]
        ws.cell(row=row_idx, column=1, value=meta["barcode"])
        ws.cell(row=row_idx, column=2, value=meta["name"])
        ws.cell(row=row_idx, column=3, value="ИП Баковец")
        for c_idx, cluster in enumerate(clusters):
            col = len(fixed_left) + 1 + c_idx
            entry = by_key.get(("ozon", cluster, sku_id))
            if entry:
                ws.cell(row=row_idx, column=col, value=entry["qty"])
        right_start = len(fixed_left) + len(clusters) + 1
        ws.cell(row=row_idx, column=right_start, value=None)         # упаковка
        ws.cell(row=row_idx, column=right_start + 1, value=None)     # примечание
        ws.cell(row=row_idx, column=right_start + 2, value=meta["article"])
        ws.cell(row=row_idx, column=right_start + 3, value=None)     # адрес+таймслот
        for c in range(1, len(headers) + 1):
            ws.cell(row=row_idx, column=c).border = border
        row_idx += 1

    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 36
    ws.column_dimensions["C"].width = 14
