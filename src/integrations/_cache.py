"""Файловый кэш для долгоживущих данных API (склады, кластеры).

Используется когда API имеет жёсткий rate-limit и данные меняются редко.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

from src.config import DATA_DIR

logger = logging.getLogger("integrations.cache")

_CACHE_DIR = DATA_DIR / "cache"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def cache_path(name: str) -> Path:
    return _CACHE_DIR / f"{name}.json"


def cache_get(name: str, max_age_sec: int) -> Optional[Any]:
    """Прочитать из кэша если не старше max_age_sec. Иначе None."""
    p = cache_path(name)
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        ts = raw.get("ts", 0)
        if time.time() - ts > max_age_sec:
            return None
        return raw.get("data")
    except Exception as e:
        logger.warning("cache_get(%s) failed: %s", name, e)
        return None


def cache_get_stale(name: str) -> Optional[Any]:
    """Прочитать любую (даже протухшую) версию кэша — для fallback при 429."""
    p = cache_path(name)
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return raw.get("data")
    except Exception:
        return None


def cache_age_sec(name: str) -> Optional[int]:
    """Сколько секунд кэшу. None если нет файла."""
    p = cache_path(name)
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return int(time.time() - raw.get("ts", 0))
    except Exception:
        return None


def cache_set(name: str, data: Any) -> None:
    p = cache_path(name)
    try:
        p.write_text(
            json.dumps({"ts": time.time(), "data": data}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning("cache_set(%s) failed: %s", name, e)
