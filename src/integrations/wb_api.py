"""Wildberries API клиент (минимальный, read-only).

Документация: https://openapi.wildberries.ru/

Auth header: Authorization: <JWT-токен>

Хосты:
  supplies-api.wildberries.ru   — поставки (warehouses)
  common-api.wildberries.ru     — тарифы приёмки (coefficients перенесён сюда)
  marketplace-api.wildberries.ru — склады продавца
  statistics-api.wildberries.ru — остатки, заказы (жёсткий лимит ~1 req/min)
  content-api.wildberries.ru    — карточки товаров (мягкий лимит)
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("integrations.wb")

WB_SUPPLIES = "https://supplies-api.wildberries.ru"
WB_COMMON = "https://common-api.wildberries.ru"
WB_MARKETPLACE = "https://marketplace-api.wildberries.ru"
WB_STATISTICS = "https://statistics-api.wildberries.ru"
WB_CONTENT = "https://content-api.wildberries.ru"


class WBAPIError(Exception):
    pass


class WBClient:
    # Глобальные кэши на уровне класса — переживают пересоздание клиентов между командами
    _CACHE_WAREHOUSES: tuple = (0.0, None)         # (ts, data) TTL 600 сек
    _CACHE_COEFS: Dict[str, tuple] = {}            # ids_key → (ts, data) TTL 90 сек

    def __init__(self, api_key: str, timeout: float = 30.0):
        if not api_key:
            raise WBAPIError("Не задан API_KEY для WB")
        self.api_key = api_key
        self.timeout = timeout
        self._headers = {
            "Authorization": api_key,
            "Content-Type": "application/json",
        }

    async def _get(self, base: str, path: str, params: Optional[Dict[str, Any]] = None,
                   retries_on_429: int = 2) -> Any:
        """GET с retry на 429. Между ретраями паузы 5, 15 сек."""
        url = f"{base}{path}"
        last_err = None
        for attempt in range(retries_on_429 + 1):
            logger.info("WB GET %s params=%s (attempt %d)", url, params, attempt + 1)
            async with httpx.AsyncClient(timeout=self.timeout) as cli:
                r = await cli.get(url, headers=self._headers, params=params or {})
            logger.info("WB %s → %s (%d bytes)", path, r.status_code, len(r.content or b""))
            if r.status_code == 429:
                # WB иногда шлёт Retry-After или X-RateLimit-Reset
                retry_after_hdr = r.headers.get("Retry-After") or r.headers.get("X-RateLimit-Reset")
                last_err = WBAPIError(
                    f"429 {path}: лимит WB"
                    + (f" (Retry-After={retry_after_hdr})" if retry_after_hdr else "")
                )
                if attempt < retries_on_429:
                    # Honor Retry-After если есть, иначе экспоненциальный
                    try:
                        wait_s = int(retry_after_hdr) if retry_after_hdr else 10 + attempt * 30
                    except ValueError:
                        wait_s = 10 + attempt * 30
                    wait_s = min(wait_s, 90)  # cap 90 сек
                    logger.info("WB 429 → wait %d sec and retry (attempt %d/%d)",
                                wait_s, attempt + 1, retries_on_429)
                    await asyncio.sleep(wait_s)
                    continue
                raise last_err
            if r.status_code >= 400:
                raise WBAPIError(f"{r.status_code} {path}: {r.text[:300]}")
            try:
                return r.json()
            except Exception:
                return r.text
        if last_err:
            raise last_err
        return None

    async def _post(self, base: str, path: str, payload: Dict[str, Any]) -> Any:
        url = f"{base}{path}"
        logger.info("WB POST %s", url)
        async with httpx.AsyncClient(timeout=self.timeout) as cli:
            r = await cli.post(url, headers=self._headers, json=payload)
        logger.info("WB %s → %s (%d bytes)", path, r.status_code, len(r.content or b""))
        if r.status_code == 429:
            raise WBAPIError(f"429 Too Many Requests. {path} лимитирован.")
        if r.status_code >= 400:
            raise WBAPIError(f"{r.status_code} {path}: {r.text[:300]}")
        try:
            return r.json()
        except Exception:
            return r.text

    # ── Список складов WB (для поставок) ────────────────────────────────────

    async def warehouses(self, *, allow_stale: bool = True) -> List[Dict[str, Any]]:
        """GET /api/v1/warehouses — список приёмочных складов WB.

        Двухуровневый кэш:
          1) in-memory 10 мин
          2) файловый 24 часа (для переживания 429 и рестартов)
        Если API возвращает 429 и есть stale-кэш — отдаём его.
        """
        import time as _t
        from src.integrations._cache import cache_get, cache_get_stale, cache_set, cache_age_sec

        # in-memory
        ts, cached = WBClient._CACHE_WAREHOUSES
        if cached is not None and (_t.time() - ts) < 600:
            logger.info("WB warehouses in-memory hit (%d sec old)", int(_t.time() - ts))
            return cached

        # файловый кэш
        file_cached = cache_get("wb_warehouses", max_age_sec=86400)
        if file_cached is not None:
            age = cache_age_sec("wb_warehouses") or 0
            logger.info("WB warehouses file cache hit (%d sec old)", age)
            WBClient._CACHE_WAREHOUSES = (_t.time(), file_cached)
            return file_cached

        # запрос к API
        try:
            data = await self._get(WB_SUPPLIES, "/api/v1/warehouses")
            result = data if isinstance(data, list) else []
            cache_set("wb_warehouses", result)
            WBClient._CACHE_WAREHOUSES = (_t.time(), result)
            return result
        except WBAPIError as e:
            if "429" in str(e) and allow_stale:
                stale = cache_get_stale("wb_warehouses")
                if stale:
                    age_h = (cache_age_sec("wb_warehouses") or 0) / 3600
                    logger.warning("WB warehouses 429 → using stale cache (%.1fh old)", age_h)
                    return stale
            raise

    # ── Коэффициенты приёмки (стоимость + доступность) ──────────────────────

    async def acceptance_coefficients(
        self, warehouse_ids: Optional[List[int]] = None, *, allow_stale: bool = True
    ) -> List[Dict[str, Any]]:
        """GET /api/tariffs/v1/acceptance/coefficients на common-api.
        WB перенёс endpoint из supplies-api (Feb 2026). Токен — любая категория.
        coefficient: -1 недоступен, 0 бесплатно, >0 платно.
        Кэш: in-memory 90 сек, файловый stale-fallback при 429.
        """
        import time as _t
        from src.integrations._cache import cache_get_stale, cache_set, cache_age_sec

        key = ",".join(sorted(str(i) for i in (warehouse_ids or []))) or "all"
        cache_name = f"wb_coefs_{key[:50]}"

        # in-memory
        cached = WBClient._CACHE_COEFS.get(key)
        if cached and (_t.time() - cached[0]) < 90:
            logger.info("WB coefs in-memory hit (key=%s, %d sec old)", key[:30], int(_t.time() - cached[0]))
            return cached[1]

        params = {}
        if warehouse_ids:
            params["warehouseIDs"] = ",".join(str(i) for i in warehouse_ids)

        try:
            data = await self._get(WB_COMMON, "/api/tariffs/v1/acceptance/coefficients", params)
            result = data if isinstance(data, list) else []
            WBClient._CACHE_COEFS[key] = (_t.time(), result)
            cache_set(cache_name, result)
            return result
        except WBAPIError as e:
            if "429" in str(e) and allow_stale:
                stale = cache_get_stale(cache_name)
                if stale is not None:
                    age_min = (cache_age_sec(cache_name) or 0) / 60
                    logger.warning("WB coefs 429 → stale cache (%.1f min old)", age_min)
                    return stale
            raise

    # ── Опции приёмки: какие склады готовы принять конкретные товары ─────────

    async def acceptance_options(
        self, goods: List[Dict[str, Any]], *, warehouse_id: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """POST /api/v1/acceptance/options на supplies-api.
        goods: [{"barcode": str, "quantity": int}, ...]
        Возвращает: [{"barcode": ..., "warehouses": [{warehouseID, canBox, canMonopallet, canSupersafe}], "isError": ...}]
        Лимит: 6 req/min.
        """
        path = "/api/v1/acceptance/options"
        url = f"{WB_SUPPLIES}{path}"
        params = {"warehouseID": warehouse_id} if warehouse_id else None
        logger.info("WB POST %s (goods=%d)", url, len(goods))
        async with httpx.AsyncClient(timeout=self.timeout) as cli:
            r = await cli.post(url, headers=self._headers, params=params or {}, json=goods)
        logger.info("WB %s → %s (%d bytes)", path, r.status_code, len(r.content or b""))
        if r.status_code == 429:
            raise WBAPIError(f"429 {path}: лимит WB (6/min)")
        if r.status_code >= 400:
            raise WBAPIError(f"{r.status_code} {path}: {r.text[:300]}")
        data = r.json()
        return data.get("result", []) if isinstance(data, dict) else []

    # ── Остатки на складах WB ───────────────────────────────────────────────

    async def stocks(self, date_from: str) -> List[Dict[str, Any]]:
        """GET /api/v1/supplier/stocks?dateFrom=YYYY-MM-DD (statistics-api).
        ⚠ Жёсткий лимит ~1 req/min. Возвращает: nmId, supplierArticle, barcode, warehouseName, quantity.
        """
        params = {"dateFrom": date_from}
        data = await self._get(WB_STATISTICS, "/api/v1/supplier/stocks", params)
        return data if isinstance(data, list) else []

    # ── Content API: каталог карточек (для линковки SKU ↔ nmID) ─────────────

    async def cards_list(self, limit_total: int = 5000) -> List[Dict[str, Any]]:
        """POST /content/v2/get/cards/list — все карточки с nmID + barcode.
        Лимит мягкий (~100 req/min). Пагинация через cursor.
        """
        items: List[Dict[str, Any]] = []
        cursor: Dict[str, Any] = {"limit": 100}
        while True:
            payload = {
                "settings": {
                    "cursor": cursor,
                    "filter": {"withPhoto": -1},
                }
            }
            data = await self._post(WB_CONTENT, "/content/v2/get/cards/list", payload)
            cards = data.get("cards", []) if isinstance(data, dict) else []
            items.extend(cards)
            cur_data = data.get("cursor", {}) if isinstance(data, dict) else {}
            total = cur_data.get("total", 0)
            if not cards or total < cursor["limit"] or len(items) >= limit_total:
                break
            # cursor для след. страницы — updatedAt + nmID последней карточки
            last = cards[-1]
            cursor = {
                "limit": 100,
                "updatedAt": last.get("updatedAt"),
                "nmID": last.get("nmID"),
            }
        return items[:limit_total]
