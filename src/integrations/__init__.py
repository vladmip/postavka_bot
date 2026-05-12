"""Интеграции с маркетплейсами (read-only API).

Контур A в стратегии docx, неделя 4. Только чтение: остатки, склады, коэффициенты.
Запись (бронь слотов, загрузка описей) — следующая итерация (неделя 7).
"""

from .ozon_api import OzonClient, OzonAPIError
from .wb_api import WBClient, WBAPIError

__all__ = [
    "OzonClient", "OzonAPIError",
    "WBClient", "WBAPIError",
]
