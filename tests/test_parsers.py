"""Golden-tests на реальных файлах из переписки/files."""
import os
from pathlib import Path

import pytest

from src.config import REFERENCE_FILES_DIR
from src.parsers import (
    classify_file, FileKind,
    parse_opis_wb, parse_opis_ozon, parse_prihod, parse_ostatki,
)


def _find(prefix: str) -> Path:
    """Найти первый файл в REFERENCE_FILES_DIR начинающийся на prefix."""
    if not REFERENCE_FILES_DIR.exists():
        pytest.skip(f"REFERENCE_FILES_DIR не найдена: {REFERENCE_FILES_DIR}")
    for f in os.listdir(REFERENCE_FILES_DIR):
        if f.startswith(prefix):
            return REFERENCE_FILES_DIR / f
    pytest.skip(f"golden-файл с префиксом {prefix!r} не найден")


def test_classify_opis_wb():
    p = _find("Опись для заливки в WB")
    assert classify_file(p) == FileKind.OPIS_WB


def test_classify_opis_ozon():
    p = _find("МСК Опись для заливки в OZ")
    assert classify_file(p) == FileKind.OPIS_OZON


def test_classify_prihod():
    p = _find("prihod-")
    assert classify_file(p) == FileKind.PRIHOD


def test_classify_ostatki():
    p = _find("Остатки Баковец")
    assert classify_file(p) == FileKind.OSTATKI


def test_parse_opis_wb_03907():
    items = parse_opis_wb(_find("Опись для заливки в WB - 03907"))
    assert len(items) >= 3
    by_bc = {i.barcode: i for i in items}
    assert "5CHOC-CARAMEL" in by_bc
    assert by_bc["5CHOC-CARAMEL"].qty == 24
    assert by_bc["MILK-CHOCOLATE"].qty == 102
    assert all(i.box_label.startswith("LBR_") for i in items)


def test_parse_opis_ozon_03909():
    items = parse_opis_ozon(_find("МСК Опись для заливки в OZ - 03909"))
    assert len(items) >= 3
    bcs = {i.barcode for i in items}
    assert "COOKIES-CREME" in bcs
    assert "3CHOC" in bcs
    for i in items:
        assert i.qty > 0
        assert i.box_label  # ШК ГМ должен быть


def test_parse_prihod():
    doc = parse_prihod(_find("prihod-01152"))
    assert doc.items, "приходная пустая"
    assert all(i.article and i.qty > 0 for i in doc.items)
    arts = {i.article for i in doc.items}
    assert any("HAIDILAO" in a for a in arts)


def test_parse_ostatki():
    items = parse_ostatki(_find("Остатки Баковец 06.05"))
    assert len(items) >= 5
    arts = {i.article for i in items}
    assert "KINDER-JOY-GP-1P" in arts
    for i in items:
        assert i.balance >= 0
