from enum import Enum
from pathlib import Path


class FileKind(str, Enum):
    OPIS_WB = "opis_wb"
    OPIS_OZON = "opis_ozon"
    PRIHOD = "prihod"
    OSTATKI = "ostatki"
    UNKNOWN = "unknown"


def classify_file(path: str | Path) -> FileKind:
    """Определяет тип входящего файла по имени.

    Эвристика по реальным именам из переписки с ЛЕБЕР:
      'Опись для заливки в WB - 03907.xlsx' → OPIS_WB
      'МСК Опись для заливки в OZ - 03909.xlsx' → OPIS_OZON
      'prihod-01187.xls' → PRIHOD
      'Остатки Баковец 06.05.xls' → OSTATKI
    """
    name = Path(path).name.lower()

    if "опись" in name and ("в wb" in name or " wb " in name or " вб " in name):
        return FileKind.OPIS_WB
    if "опись" in name and ("в oz" in name or " oz " in name or "озон" in name):
        return FileKind.OPIS_OZON
    if name.startswith("prihod") or "приходная" in name:
        return FileKind.PRIHOD
    if name.startswith("остатки") or "stock" in name:
        return FileKind.OSTATKI

    return FileKind.UNKNOWN
