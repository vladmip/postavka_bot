"""Ozon Seller API клиент.

Документация: https://docs.ozon.ru/api/seller/

Auth headers:
  Client-Id: <CLIENT_ID>
  Api-Key:   <API_KEY>

Rate limits Ozon обычно мягкие, но некоторые endpoints (draft/create, cluster/list)
имеют лимит 1 req/sec. Делаем retry на 429.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("integrations.ozon")

OZON_BASE = "https://api-seller.ozon.ru"


class OzonAPIError(Exception):
    pass


# Ozon после 16.03.2026 перешёл с integer-кодов на string-enum для FBO draft.
# Старый int-код в payload → Ozon молча трактует как UNSPECIFIED → scoring 404.
_SUPPLY_TYPE_ENUM = {1: "CROSSDOCK", 2: "DIRECT", 3: "MULTI_CLUSTER"}


def _supply_type_str(value: Any) -> str:
    if isinstance(value, str):
        return value
    return _SUPPLY_TYPE_ENUM.get(int(value), "DIRECT")


class OzonClient:
    # Cooldown по endpoint: после 429 не дёргать N сек.
    # Применяется только к account-level лимитам (draft/*/create — 2/мин, 50/час).
    # Для глобально лимитированных (timeslot/info — 2/сек на ВСЕХ продавцов)
    # 5-мин cooldown бесполезен: лимит делится с другими, надо просто упорно ретраить.
    _COOLDOWN: Dict[str, float] = {}   # path → unix-ts когда можно снова
    _COOLDOWN_SEC = 300  # 5 минут

    # Endpoints с ГЛОБАЛЬНЫМ rate-limit (общий на всех продавцов).
    # Их НЕ кулдауним; вместо этого долбим короткими ретраями.
    # Источник: тикет Ozon SS + наблюдения (code:8 "request rate limit per second").
    _GLOBAL_LIMIT_PATHS = frozenset({
        "/v1/draft/timeslot/info",
        "/v2/draft/timeslot/info",
        "/v1/supply-order/timeslot/update",
        "/v1/draft/create/info",
        "/v2/draft/create/info",
        "/v1/draft/supply/create",
        "/v1/draft/supply/create/info",
        # v2 финализация — с 16.03.2026 заменяет /v1/draft/supply/create.
        "/v2/draft/supply/create",
        "/v2/draft/supply/create/status",
        # Новые сабпути draft_create (после 16.03.2026 заменили /v1/draft/create).
        # Ozon отдаёт по ним code:8 «per second» — это глобальный лимит, не
        # account-ban. Лечится коротким backoff (2-5с × 5 ретраев), а не одним
        # 30-секундным.
        "/v1/draft/crossdock/create",
        "/v1/draft/direct/create",
        "/v1/draft/multi-cluster/create",
    })

    def __init__(self, client_id: str, api_key: str, timeout: float = 30.0,
                 proxy: Optional[str] = None):
        if not client_id or not api_key:
            raise OzonAPIError("Не задан CLIENT_ID или API_KEY для Ozon")
        self.client_id = client_id
        self.api_key = api_key
        self.timeout = timeout
        self.proxy = proxy  # 'http://user:pass@host:port' или None
        self._headers = {
            "Client-Id": client_id,
            "Api-Key": api_key,
            "Content-Type": "application/json",
        }

    async def _request(self, method: str, path: str, *, json_body=None, params=None,
                       retries_on_429: Optional[int] = None) -> Dict[str, Any]:
        """Универсальный запрос с retry на 429.

        Две стратегии:
        - account-level лимит (draft/*/create): 1 ретрай через 30с, потом 5-мин cooldown
        - глобальный лимит (timeslot/info): 8 ретраев с короткой паузой 1.5-3с,
          без cooldown (бесполезен — лимит общий с другими продавцами).
        """
        import time as _t
        is_global = path in OzonClient._GLOBAL_LIMIT_PATHS
        # Anti-abuse: только для endpoints, по которым Ozon SS подтверждали
        # account-level бан. Дефолтно ставим долгий cooldown ТОЛЬКО если
        # body 429 говорит о бане (не «per second»). См. ниже sniff body.
        anti_abuse_paths = {
            "/v1/draft/supply/create/info",
        }
        # /v1/draft/supply/create раньше тоже сидел тут, но Ozon отдаёт по нему
        # `code:8 "request rate limit per second"` — это глобальный per-second
        # лимит, а не account-ban. Лечится backoff'ом, как timeslot/info.
        is_anti_abuse = path in anti_abuse_paths

        # «Тяжёлые» эндпоинты: draft/*/create — Ozon per-second лимит для них
        # часто загружен другими продавцами на 15-30 сек подряд. Короткий
        # backoff 2-5с не успевает дождаться. Длинный backoff 5-45с × 6 ретраев
        # даёт окно ~125с — достаточно чтобы лимит «отдышался».
        heavy_limit_paths = {
            "/v1/draft/crossdock/create",
            "/v1/draft/direct/create",
            "/v1/draft/multi-cluster/create",
            "/v2/draft/supply/create",
        }
        is_heavy = path in heavy_limit_paths

        if retries_on_429 is None:
            # Для anti-abuse — 0 (любой ретрай продлевает бан).
            # Для heavy-limit — 2 ретрая (общее окно ~60с). Большее долбить
            # бесполезно: наши попытки сами участвуют в перегрузке per-second
            # лимита Ozon. После исчерпания — отдаём юзеру ❌, пусть нажмёт
            # повтор через 1-2 мин (за это время другие продавцы отпустят).
            # Для остальных global-limit — 5 ретраев с коротким backoff (~20с).
            # Для account-level (не global) — 1.
            if is_anti_abuse:
                retries_on_429 = 0
            elif is_heavy:
                retries_on_429 = 2
            elif is_global:
                retries_on_429 = 5
            else:
                retries_on_429 = 1
        # Backoff: heavy — длинный 20+40 (~60с окно), global — короткий 2-5с,
        # account-level — единственный 30с.
        if is_heavy:
            backoff: List[int] = [20, 40]
        elif is_global:
            backoff = [2, 3, 4, 5, 5, 5]
        else:
            backoff = [30]

        # Cooldown check — persistent (файл), переживает рестарт бота
        from src.integrations._cache import cooldown_remaining, cooldown_set
        # Кулдауним: anti-abuse, account-level (не global), и heavy
        # (per-second лимит у нас обычно — мягкий аккаунт-бан Ozon, не
        # глобальный шум, наши новые попытки его продлевают).
        if is_anti_abuse or not is_global or is_heavy:
            remaining = cooldown_remaining(path)
            if remaining > 0:
                wait_min = remaining // 60
                wait_sec = remaining % 60
                raise OzonAPIError(
                    f"Cooldown на {path}: подожди {wait_min}м {wait_sec}с. "
                    f"Каждый запрос во время cooldown может продлить rate-limit на стороне Ozon."
                )

        url = f"{OZON_BASE}{path}"
        client_kwargs: Dict[str, Any] = {"timeout": self.timeout}
        if self.proxy:
            if self.proxy.lower().startswith(("socks4://", "socks5://", "socks5h://")):
                # SOCKS-прокси через httpx-socks с remote DNS (rdns=True).
                # httpx-socks не понимает schema 'socks5h', нормализуем на 'socks5'.
                from httpx_socks import AsyncProxyTransport
                pxy = self.proxy
                if pxy.lower().startswith("socks5h://"):
                    pxy = "socks5://" + pxy[len("socks5h://"):]
                client_kwargs["transport"] = AsyncProxyTransport.from_url(pxy, rdns=True)
            else:
                client_kwargs["proxy"] = self.proxy
        # Краткий payload для логов (без credentials — они в headers).
        import json as _json
        payload_preview = ""
        if method == "POST" and json_body is not None:
            try:
                payload_preview = _json.dumps(json_body, ensure_ascii=False)
            except Exception:
                payload_preview = repr(json_body)
            if len(payload_preview) > 600:
                payload_preview = payload_preview[:600] + "…"
        elif method == "GET" and params:
            payload_preview = repr(params)[:300]

        for attempt in range(retries_on_429 + 1):
            logger.info(
                "Ozon %s %s (attempt %d/%d) payload=%s",
                method, path, attempt + 1, retries_on_429 + 1, payload_preview,
            )
            async with httpx.AsyncClient(**client_kwargs) as cli:
                if method == "POST":
                    r = await cli.post(url, headers=self._headers, json=json_body or {})
                else:
                    r = await cli.get(url, headers=self._headers, params=params or {})
            body_preview = (r.text or "")[:800].replace("\n", " ")
            logger.info(
                "Ozon %s → %s (%d bytes) body=%s",
                path, r.status_code, len(r.content or b""), body_preview,
            )
            if r.status_code == 429:
                rl_headers = {k: v for k, v in r.headers.items()
                              if "limit" in k.lower() or "retry" in k.lower() or "rate" in k.lower()}
                logger.warning("Ozon 429 headers: %s | body: %s", rl_headers, r.text[:200])
                if attempt < retries_on_429:
                    ra = r.headers.get("Retry-After")
                    try:
                        wait_s = int(ra) if ra else backoff[min(attempt, len(backoff) - 1)]
                    except ValueError:
                        wait_s = backoff[min(attempt, len(backoff) - 1)]
                    logger.warning("Ozon 429 on %s → wait %ds (attempt %d/%d)",
                                   path, wait_s, attempt + 1, retries_on_429)
                    await asyncio.sleep(wait_s)
                    continue
                # Закончились ретраи — решаем по body, это per-second лимит или account-ban.
                body_lower = (r.text or "").lower()
                is_per_second = "per second" in body_lower or '"code":8' in body_lower
                if is_anti_abuse and not is_per_second:
                    cooldown_set(path, 15 * 60)
                    raise OzonAPIError(
                        f"Ozon 429 на {path}: anti-abuse rate limit. "
                        f"Cooldown 15 мин — каждый ретрай продлевает бан."
                    )
                if is_global or is_per_second:
                    # Heavy paths: после исчерпанной серии 429 ставим локальный
                    # cooldown 5 мин — Ozon явно нас «мягко банит» (часто из-за
                    # серии невалидных drafts), наши новые попытки только
                    # продлевают ban. Лучше переждать.
                    if is_heavy:
                        cooldown_set(path, 5 * 60)
                        raise OzonAPIError(
                            f"Ozon 429 на {path}: rate limit. "
                            f"Cooldown 5 мин — подожди и повтори. "
                            f"Если повторяется — проверь, что у товаров заполнены "
                            f"габариты в Ozon Seller Center."
                        )
                    raise OzonAPIError(
                        f"Ozon 429 на {path}: request rate limit per second. "
                        f"Повтори через 30-60 сек."
                    )
                cooldown_set(path, OzonClient._COOLDOWN_SEC)
                raise OzonAPIError(
                    f"Ozon 429 на {path}: account-level rate limit. "
                    f"Cooldown {OzonClient._COOLDOWN_SEC // 60} мин."
                )
            if r.status_code >= 400:
                raise OzonAPIError(f"{r.status_code} {path}: {r.text[:800]}")
            return r.json()
        raise OzonAPIError(f"Unexpected fallthrough on {path}")

    async def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return await self._request("POST", path, json_body=payload)

    async def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return await self._request("GET", path, params=params)

    # ── stocks ─────────────────────────────────────────────────────────────

    async def analytics_stocks(
        self,
        skus: List[str],
        *,
        turnover_grades: Optional[List[str]] = None,
        cluster_ids: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """`POST /v1/analytics/stocks` — Ozon-аналитика по остаткам с уже
        посчитанными метриками: ads (средние продажи/день), idc (дни покрытия),
        requested_stock_count (рекомендация к поставке), excess_stock_count,
        turnover_grade (DEFICIT/POPULAR/ACTUAL/SURPLUS/...).

        Возвращает items per (sku, cluster) — один SKU может быть в нескольких
        кластерах, agregуй на стороне caller'а если нужно.
        Обновляется на стороне Ozon 2 раза в день (07:00 и 16:00 UTC).
        Лимит 1-100 SKU за раз — caller сам бьёт пачки.
        """
        if not skus:
            return []
        payload: Dict[str, Any] = {
            "skus": [str(s) for s in skus[:100]],
            "turnover_grades": turnover_grades or [],
        }
        if cluster_ids:
            payload["cluster_ids"] = [str(c) for c in cluster_ids]
        data = await self._post("/v1/analytics/stocks", payload)
        return data.get("items", []) or []

    async def stocks_fbo(self, limit: int = 1000) -> List[Dict[str, Any]]:
        """Остатки FBO: /v4/product/info/stocks (с пагинацией через cursor)."""
        items: List[Dict[str, Any]] = []
        cursor = ""
        while True:
            payload = {
                "cursor": cursor,
                "filter": {"visibility": "ALL"},
                "limit": min(limit, 1000),
            }
            data = await self._post("/v4/product/info/stocks", payload)
            chunk = data.get("items", [])
            items.extend(chunk)
            cursor = data.get("cursor", "") or ""
            if not cursor or len(chunk) == 0 or len(items) >= limit:
                break
        return items[:limit]

    # ── warehouses (FBO кластеры) ───────────────────────────────────────────

    async def cluster_list(self, *, allow_stale: bool = True) -> List[Dict[str, Any]]:
        """Список FBO-кластеров с складами: /v1/cluster/list.
        Кэш файловый 24ч (кластеры меняются раз в неделю и реже).
        """
        from src.integrations._cache import cache_get, cache_get_stale, cache_set, cache_age_sec
        cached = cache_get("ozon_clusters", max_age_sec=86400)
        if cached is not None:
            age = cache_age_sec("ozon_clusters") or 0
            logger.info("Ozon clusters from cache (%d sec old)", age)
            return cached
        try:
            data = await self._post("/v1/cluster/list", {"cluster_type": "CLUSTER_TYPE_OZON"})
            result = data.get("clusters", [])
            cache_set("ozon_clusters", result)
            return result
        except OzonAPIError as e:
            if "429" in str(e) and allow_stale:
                stale = cache_get_stale("ozon_clusters")
                if stale is not None:
                    logger.warning("Ozon clusters 429 → stale cache")
                    return stale
            raise

    async def warehouse_list(self) -> List[Dict[str, Any]]:
        """Список FBS-складов поставщика (если есть): /v1/warehouse/list."""
        data = await self._post("/v1/warehouse/list", {})
        return data.get("result", [])

    # ── каталог товаров (для линковки SKU) ──────────────────────────────────

    async def product_list(self, limit: int = 5000) -> List[Dict[str, Any]]:
        """Все offer_id + product_id с пагинацией: /v3/product/list."""
        items: List[Dict[str, Any]] = []
        last_id = ""
        while True:
            payload = {
                "filter": {"visibility": "ALL"},
                "last_id": last_id,
                "limit": min(1000, limit - len(items)),
            }
            data = await self._post("/v3/product/list", payload)
            result = data.get("result", {})
            chunk = result.get("items", [])
            items.extend(chunk)
            last_id = result.get("last_id", "")
            if not last_id or not chunk or len(items) >= limit:
                break
        return items[:limit]

    async def product_info_list(self, product_ids: List[int]) -> List[Dict[str, Any]]:
        """Детали (включая barcode) пачкой: /v3/product/info/list."""
        if not product_ids:
            return []
        out: List[Dict[str, Any]] = []
        # API ограничивает batch ~1000; берём 500 для безопасности
        for i in range(0, len(product_ids), 500):
            chunk = product_ids[i:i + 500]
            data = await self._post(
                "/v3/product/info/list",
                {"product_id": chunk, "sku": [], "offer_id": []},
            )
            out.extend(data.get("result", {}).get("items", []) or data.get("items", []) or [])
        return out

    # ── Draft API: создание поставки FBO ─────────────────────────────────────
    # /v1/draft/create отключён 16.03.2026 → используем новые endpoints.
    # Лимиты: 2 req/min, 50 req/hour, 500 req/day. Draft живёт 30 минут.

    async def draft_create(
        self,
        items: List[Dict[str, Any]],
        cluster_ids: Optional[List[int]] = None,
        draft_type: str = "CREATE_TYPE_CROSSDOCK",
        drop_off_point_warehouse_id: Optional[int] = None,
    ) -> str:
        """Создать черновик поставки. Выбирает endpoint по типу:
        - multi-cluster если cluster_ids > 1
        - crossdock или direct по draft_type

        items: [{"sku": int, "quantity": int}]
        Возвращает operation_id.

        Новые endpoints (с 03.2026): items + macrolocal_cluster_id внутри cluster_info.
        deletion_sku_mode: "PARTIAL" (не дропать валидные SKU) или "FULL" (отклонить
        всю заявку при первой ошибке). Раньше Ozon принимал int 1 — больше нет.
        """
        payload: Dict[str, Any] = {"deletion_sku_mode": "PARTIAL"}

        if cluster_ids and len(cluster_ids) > 1:
            path = "/v1/draft/multi-cluster/create"
            payload["clusters_info"] = [
                {"macrolocal_cluster_id": int(cid), "items": items}
                for cid in cluster_ids
            ]
            if drop_off_point_warehouse_id:
                payload["drop_off_point_warehouse_id"] = drop_off_point_warehouse_id
        else:
            cid = int(cluster_ids[0]) if cluster_ids else None
            cluster_info: Dict[str, Any] = {"items": items}
            if cid:
                cluster_info["macrolocal_cluster_id"] = cid
            if drop_off_point_warehouse_id:
                cluster_info["drop_off_point_warehouse_id"] = drop_off_point_warehouse_id
            payload["cluster_info"] = cluster_info
            if "CROSSDOCK" in (draft_type or "").upper():
                path = "/v1/draft/crossdock/create"
                # CROSSDOCK = DROPOFF: товар везут в drop_off_warehouse,
                # Ozon развозит по конечным РФЦ. Хаб обязательно передаётся
                # вызывающим — через drop_off_point_warehouse_id.
                if not drop_off_point_warehouse_id:
                    raise OzonAPIError(
                        "Для CROSSDOCK обязателен drop_off_point_warehouse_id "
                        "(выбери точку отгрузки в боте перед draft_create)."
                    )
                payload["delivery_info"] = {
                    "type": "DROPOFF",
                    "drop_off_warehouse": {
                        "warehouse_id": int(drop_off_point_warehouse_id),
                        "warehouse_type": "DELIVERY_POINT",
                    },
                }
            else:
                path = "/v1/draft/direct/create"
        data = await self._post(path, payload)
        # Новые endpoints (03.2026) синхронные: возвращают draft_id напрямую
        # либо errors[]. Старые endpoints возвращали operation_id для polling.
        # Возвращаем как строку, помеченную префиксом для различения:
        # 'sync:<draft_id>' — синхронный успех
        # '<operation_id>'   — асинхронный (нужен polling)
        # 'err:<reason>'     — ошибка
        if data.get("errors"):
            errs = data["errors"]
            reasons = []
            for e in errs:
                rs = e.get("error_reasons") or []
                msg = e.get("error_message") or e.get("message") or ""
                reasons.append(f"{msg}/{','.join(rs)}")
            raise OzonAPIError(f"draft/create errors: {'; '.join(reasons)[:400]}")
        if data.get("draft_id"):
            return f"sync:{data['draft_id']}"
        op = data.get("operation_id") or data.get("task_id") or ""
        if not op:
            logger.warning("Ozon %s no draft_id/operation_id: %s", path, str(data)[:500])
        return str(op)

    async def draft_create_info(
        self,
        operation_id: str = "",
        draft_id: int = 0,
        retries_on_429: int = 0,
    ) -> Dict[str, Any]:
        """Получить детали draft'а.

        Возвращает clusters[].warehouses[] с полями is_available, total_score,
        bundle_ids — то самое scoring от Ozon.

        Новые sync endpoints (с 16.03.2026) возвращают draft_id и требуют
        POST /v2/draft/create/info. Старый POST /v1/draft/create/info оставлен
        только для legacy operation_id.

        429 здесь = глобальный per-second лимит Ozon. Внутренних ретраев НЕТ
        (default retries_on_429=0): частые быстрые попытки только усугубляют
        перегруз и тратят наши слоты. Внешний _fetch_scoring_persistent шлёт
        запросы раз в 60-90с с jitter — это и есть «спокойный режим».
        """
        if draft_id:
            return await self._request(
                "POST", "/v2/draft/create/info",
                json_body={"draft_id": int(draft_id)},
                retries_on_429=retries_on_429,
            )
        if not operation_id:
            raise OzonAPIError("draft_create_info требует draft_id или operation_id")
        return await self._request(
            "POST", "/v1/draft/create/info",
            json_body={"operation_id": operation_id},
            retries_on_429=retries_on_429,
        )

    # Тумблер версии endpoint timeslot/info. Ozon SS говорил что v2 имеет
    # 2 req/sec на всех — но v1 у нас тоже 429. Пробуем v2 как альтернативу.
    TIMESLOT_INFO_PATH = "/v2/draft/timeslot/info"

    async def draft_timeslot_info(
        self,
        draft_id: int,
        date_from: str,
        date_to: str,
        warehouse_ids: Optional[List[int]] = None,
        cluster_id: Optional[int] = None,
        supply_type: int = 2,  # 1=CROSSDOCK, 2=DIRECT (порядок выявлен экспериментом)
        retries_on_429: Optional[int] = None,
    ) -> Dict[str, Any]:
        """POST /v2/draft/timeslot/info (или /v1/ — см. TIMESLOT_INFO_PATH).

        date_from/date_to:
          - v2: 'YYYY-MM-DD' (без времени, проверяется regex'ом)
          - v1: 'YYYY-MM-DDTHH:MM:SSZ'
        warehouse_ids:
          - v2: упаковывается в selected_cluster_warehouses вместе с cluster_id
          - v1: передаётся как warehouse_ids
        cluster_id — macrolocal_cluster_id того же кластера что и draft (нужен для v2).
        """
        path = OzonClient.TIMESLOT_INFO_PATH
        is_v2 = "/v2/" in path
        if is_v2:
            date_from = date_from[:10]
            date_to = date_to[:10]
        payload: Dict[str, Any] = {
            "draft_id": draft_id,
            "date_from": date_from,
            "date_to": date_to,
        }
        st_str = _supply_type_str(supply_type)
        if is_v2:
            # v2 требует supply_type как enum-строку:
            # "CROSSDOCK" / "DIRECT" / "MULTI_CLUSTER" (после 16.03.2026, не int).
            payload["supply_type"] = st_str
            # selected_cluster_warehouses (1-20 items). Поле wh_id зависит от типа:
            #   DIRECT → storage_warehouse_id (destination РФЦ)
            #   CROSSDOCK → drop_off_warehouse_id (приёмочный хаб)
            #   MULTI_CLUSTER → не использует wh_id
            if not cluster_id:
                raise OzonAPIError("v2 timeslot/info требует cluster_id")
            # Поле wh-id зависит от supply_type:
            #   DIRECT → storage_warehouse_id обязателен (целевой РФЦ).
            #   CROSSDOCK → НЕ передавать wh. Хаб уже зашит в draft/crossdock/create
            #     через delivery_info.drop_off_warehouse, timeslot тянется для него.
            #     Ozon вернёт 400 "not allowed parameter warehouse_id" если передать.
            #   MULTI_CLUSTER → wh не используется.
            entry: Dict[str, Any] = {"macrolocal_cluster_id": int(cluster_id)}
            if warehouse_ids and st_str == "DIRECT":
                entry["storage_warehouse_id"] = int(warehouse_ids[0])
            payload["selected_cluster_warehouses"] = [entry]
        elif warehouse_ids:
            payload["warehouse_ids"] = warehouse_ids
        return await self._request(
            "POST", path,
            json_body=payload, retries_on_429=retries_on_429,
        )

    async def draft_supply_create(
        self,
        draft_id: int,
        timeslot_from: str,
        timeslot_to: str,
        warehouse_id: int,
    ) -> str:
        """POST /v1/draft/supply/create — финализировать draft в реальную поставку.
        Возвращает operation_id.

        Timestamp нормализуется: Ozon возвращает в timeslot/info без Z (например
        "2026-05-15T16:00:00"), но protobuf здесь требует RFC 3339 с timezone (`Z`).
        """
        # Нормализуем — добавляем Z если нет timezone-маркера
        def _norm_ts(t: str) -> str:
            if not t:
                return t
            if t.endswith("Z") or "+" in t[10:] or "-" in t[10:]:
                return t
            return t + "Z"

        payload = {
            "draft_id": draft_id,
            "timeslot": {
                "from_in_timezone": _norm_ts(timeslot_from),
                "to_in_timezone": _norm_ts(timeslot_to),
            },
            "warehouse_id": warehouse_id,
        }
        data = await self._post("/v1/draft/supply/create", payload)
        return data.get("operation_id", "")

    async def draft_supply_create_info(self, operation_id: str) -> Dict[str, Any]:
        """POST /v1/draft/supply/create/info — статус финализации (legacy v1)."""
        return await self._post("/v1/draft/supply/create/info",
                                 {"operation_id": operation_id})

    async def draft_supply_create_v2(
        self,
        draft_id: int,
        cluster_id: int,
        warehouse_id: int,
        timeslot_from: str,
        timeslot_to: str,
        supply_type: Any = 2,
    ) -> List[str]:
        """POST /v2/draft/supply/create — финализация в supply (актуальный API).

        С 16.03.2026 заменяет /v1/draft/supply/create. Новый payload:
            draft_id, selected_cluster_warehouses[{macrolocal_cluster_id,
            storage_warehouse_id}], timeslot{from/to_in_timezone}, supply_type.
        Ответ sync: {draft_id, error_reasons[]}. error_reasons пустой → ack ok,
        дальше нужен polling /v2/draft/supply/create/status до status=SUCCESS.

        retries_on_429=1: финальный endpoint самый чувствительный, агрессивные
        ретраи на нём могут реально повышать риск account-level бана. Лучше
        одна вежливая попытка, дальше — пользователь жмёт «🔁 Повторить».

        ⚠ Timestamps БЕЗ Z. v2-парсер Ozon отдаёт ошибку
        `invalid from_in_timezone: parsing time "...Z": extra text: "Z"`
        если суффикс Z присутствует. В v1 наоборот — Z обязателен.
        timeslot/info v2 в response отдаёт без Z, поэтому передаём as-is.
        """
        def _strip_z(t: str) -> str:
            if t and t.endswith("Z"):
                return t[:-1]
            return t

        payload = {
            "draft_id": int(draft_id),
            "selected_cluster_warehouses": [{
                "macrolocal_cluster_id": int(cluster_id),
                "storage_warehouse_id": int(warehouse_id),
            }],
            "timeslot": {
                "from_in_timezone": _strip_z(timeslot_from),
                "to_in_timezone": _strip_z(timeslot_to),
            },
            "supply_type": _supply_type_str(supply_type),
        }
        data = await self._request(
            "POST", "/v2/draft/supply/create",
            json_body=payload, retries_on_429=1,
        )
        reasons = data.get("error_reasons") or []
        return [r for r in reasons if r and r != "UNSPECIFIED"]

    async def draft_supply_create_status_v2(self, draft_id: int) -> Dict[str, Any]:
        """POST /v2/draft/supply/create/status — статус финализации (v2).

        Возвращает {error_reasons, order_id, status}.
        status: UNSPECIFIED | SUCCESS | IN_PROGRESS | FAILED.
        """
        return await self._post("/v2/draft/supply/create/status",
                                 {"draft_id": int(draft_id)})

    # ── FBO drop-off/хабы — для FBO кроссдока (Щербинка и др.) ─────────────

    async def warehouse_fbo_list(
        self,
        supply_types: Optional[List[str]] = None,
        search: str = "",
    ) -> List[Dict[str, Any]]:
        """POST /v1/warehouse/fbo/list — список FBO drop-off точек для поставок.

        supply_types: ["CREATE_TYPE_CROSSDOCK"] для FBO-кроссдок-хабов,
                      ["CREATE_TYPE_DIRECT"] для прямых поставок.
                      По умолчанию оба типа.
        search: текст для поиска, минимум 4 символа.

        Возвращает [{address, coordinates, name, warehouse_id, warehouse_type}],
        где warehouse_type ∈ {DELIVERY_POINT, ORDERS_RECEIVING_POINT,
        SORTING_CENTER, FULL_FILLMENT, CROSS_DOCK}.
        """
        if not search or len(search) < 4:
            return []
        if not supply_types:
            supply_types = ["CREATE_TYPE_CROSSDOCK", "CREATE_TYPE_DIRECT"]
        payload = {
            "filter_by_supply_type": supply_types,
            "search": search,
        }
        data = await self._post("/v1/warehouse/fbo/list", payload)
        return data.get("search", []) or []

    # ── FBS drop-off (для поиска любимых точек кроссдока) ──────────────────

    async def warehouse_fbs_drop_off_list(
        self,
        country_code: str = "RU",
        latitude: float = 55.755826,
        longitude: float = 37.6173,
        address_search: str = "",
        types: Optional[List[str]] = None,
        is_kgt: bool = False,
    ) -> List[Dict[str, Any]]:
        """POST /v1/warehouse/fbs/create/drop-off/list — список drop-off точек.

        Возвращает [{id, address, type (PVZ/PPZ/SC), coordinates}, ...].
        Используется для поиска ПВЗ/ППЗ/СЦ по адресу при добавлении любимой точки.
        """
        payload: Dict[str, Any] = {
            "country_code": country_code,
            "is_kgt": is_kgt,
            "coordinates": {"latitude": latitude, "longitude": longitude},
            "search": {
                "address": address_search,
            },
        }
        if types:
            payload["search"]["types"] = types
        data = await self._post("/v1/warehouse/fbs/create/drop-off/list", payload)
        return data.get("points", []) or []

    # ── Возвраты (FBO/FBS Giveout) ──────────────────────────────────────────

    async def returns_list(
        self, *, filter_dict: Optional[Dict[str, Any]] = None, limit: int = 500,
    ) -> List[Dict[str, Any]]:
        """POST /v1/returns/list — универсальный список возвратов (FBO + FBS).

        Возвращает массив с полями: id, posting_number, order_id, schema (Fbo/Fbs),
        product {sku, offer_id, name, quantity, price}, place {id, name, address},
        return_reason_name, visual.status, logistic.return_date и т.п.
        """
        out: List[Dict[str, Any]] = []
        last_id = 0
        while True:
            payload: Dict[str, Any] = {"limit": min(limit, 500), "last_id": last_id}
            if filter_dict:
                payload["filter"] = filter_dict
            data = await self._post("/v1/returns/list", payload)
            chunk = data.get("returns") or []
            out.extend(chunk)
            if not data.get("has_next") or not chunk or len(out) >= limit:
                break
            last = chunk[-1].get("id")
            try:
                last_id = int(last) if last else 0
            except (ValueError, TypeError):
                last_id = 0
            if not last_id:
                break
        return out[:limit]

    async def returns_giveout_is_enabled(self) -> bool:
        """POST /v1/return/giveout/is-enabled — можно ли вообще забрать возвраты."""
        data = await self._post("/v1/return/giveout/is-enabled", {})
        return bool(data.get("enabled"))

    async def returns_giveout_list(self, limit: int = 500) -> List[Dict[str, Any]]:
        """POST /v1/return/giveout/list — список партий возвратов.

        Возвращает: [{giveout_id, giveout_status, approved_articles_count,
        total_articles_count, warehouse_address, warehouse_name, warehouse_id, created_at}]
        Статусы: UNSPECIFIED | CREATED | APPROVED | COMPLETED | CANCELLED.
        """
        out: List[Dict[str, Any]] = []
        last_id = 0
        while True:
            data = await self._post(
                "/v1/return/giveout/list",
                {"last_id": last_id, "limit": min(limit, 500)},
            )
            chunk = data.get("giveouts") or []
            out.extend(chunk)
            if not chunk or len(out) >= limit:
                break
            last_id = chunk[-1].get("giveout_id") or 0
            if not last_id:
                break
        return out[:limit]

    async def returns_giveout_info(self, giveout_id: int) -> Dict[str, Any]:
        """POST /v1/return/giveout/info — состав партии возвратов (товары внутри).

        Возвращает: {giveout_id, giveout_status, warehouse_name, warehouse_address,
        articles: [{name, approved, delivery_schema, seller_id}, ...]}
        """
        return await self._post("/v1/return/giveout/info", {"giveout_id": int(giveout_id)})

    async def returns_giveout_get_pdf(self) -> bytes:
        """POST /v1/return/giveout/get-pdf — PDF этикетки на партию возвратов.

        Ozon отдаёт application/pdf напрямую (не base64 в JSON, как в схеме доков).
        Возвращает bytes — содержимое PDF.
        """
        import httpx
        url = f"{OZON_BASE}/v1/return/giveout/get-pdf"
        client_kwargs: Dict[str, Any] = {"timeout": self.timeout}
        if self.proxy:
            if self.proxy.lower().startswith(("socks4://", "socks5://", "socks5h://")):
                from httpx_socks import AsyncProxyTransport
                pxy = self.proxy
                if pxy.lower().startswith("socks5h://"):
                    pxy = "socks5://" + pxy[len("socks5h://"):]
                client_kwargs["transport"] = AsyncProxyTransport.from_url(pxy, rdns=True)
            else:
                client_kwargs["proxy"] = self.proxy
        async with httpx.AsyncClient(**client_kwargs) as cli:
            r = await cli.post(url, headers=self._headers, json={})
        ct = (r.headers.get("content-type") or "").lower()
        logger.info("Ozon /v1/return/giveout/get-pdf → %s (%d bytes, ct=%s)",
                    r.status_code, len(r.content or b""), ct)
        if r.status_code >= 400:
            raise OzonAPIError(f"{r.status_code} /v1/return/giveout/get-pdf: {r.text[:500]}")
        # Ozon чаще всего отдаёт JSON с base64-кодированным PDF в одном
        # из полей: 'pdf' (как в реальности) или 'file_content' (как в доках).
        # Реже — сырой application/pdf. Поддерживаем все варианты.
        if "json" in ct or r.content[:1] == b"{":
            try:
                data = r.json()
                fc = data.get("pdf") or data.get("file_content") or data.get("content")
                if fc:
                    import base64
                    if fc.startswith("data:"):
                        fc = fc.split(",", 1)[-1]
                    decoded = base64.b64decode(fc, validate=False)
                    logger.info(
                        "Ozon PDF (из JSON): %d bytes, head=%r",
                        len(decoded), decoded[:8],
                    )
                    return decoded
                logger.warning("Ozon get-pdf JSON без PDF-поля: keys=%s",
                               list(data.keys())[:10])
            except Exception as e:
                logger.warning("Ozon get-pdf JSON parse failed: %s", e)
        logger.info("Ozon PDF raw: head=%r", r.content[:8])
        return r.content

    # ── FBO заказы (для аналитики rate of sale) ────────────────────────────

    async def postings_fbo_list(
        self,
        date_from: str,
        date_to: str,
        *,
        statuses: Optional[List[str]] = None,
        max_total: int = 20000,
    ) -> List[Dict[str, Any]]:
        """POST /v3/posting/fbo/list — заказы FBO за период (cursor-based).

        v2 deprecated 01.06.2026. v3 отличия:
          - пагинация через `cursor` (не offset)
          - `status` теперь массив `["awaiting_packaging", "delivered", ...]`
          - `limit` 1..100 (раньше до 1000) — больше страниц на тех же данных
          - response поля: `postings[]` + `cursor` + `has_next` (вместо `result[]`)

        date_from/date_to: ISO 'YYYY-MM-DDTHH:MM:SS.000Z' (с миллисекундами и Z).
        statuses: список фильтров; None/пусто = все.
        Возвращает posting'и: order_id, posting_number, status, in_process_at,
        products[{sku, offer_id, name, price, quantity}], analytics_data, financial_data.
        """
        out: List[Dict[str, Any]] = []
        cursor = ""
        page_size = 100  # v3 лимит — больше нельзя
        while True:
            payload: Dict[str, Any] = {
                "cursor": cursor,
                "filter": {
                    "since": date_from,
                    "to": date_to,
                },
                "limit": page_size,
                "sort_dir": "asc",
                "translit": True,
                "with": {
                    "analytics_data": False,
                    "financial_data": False,
                    "legal_info": False,
                },
            }
            if statuses:
                payload["filter"]["status"] = statuses
            data = await self._post("/v3/posting/fbo/list", payload)
            chunk = data.get("postings") or []
            if not isinstance(chunk, list):
                break
            out.extend(chunk)
            cursor = data.get("cursor", "") or ""
            if not data.get("has_next") or not cursor or len(out) >= max_total:
                break
        return out[:max_total]

    # ── Вывозы (Removal) — товар возвращается продавцу ────────────────────

    async def removal_from_stock_list(
        self,
        date_from: str,
        date_to: str,
        *,
        max_total: int = 5000,
    ) -> List[Dict[str, Any]]:
        """POST /v1/removal/from-stock/list — вывозы СО СТОКА FBO.

        Это товары которые продавец заказал вывезти со склада Ozon (или которые
        Ozon сам решил вывезти из-за длительного хранения). В ЛК — раздел
        seller.ozon.ru/app/fbo-operations/returns?tab=Stock.

        date_from/date_to: YYYY-MM-DD.
        Возвращает rows: {name, offer_id, sku, barcode, quantity_for_return,
        box_id, return_id, return_state, return_created_at, delivery_type,
        destination_warehouse_name, destination_warehouse_address, delivery_date,
        given_out_date (когда выдан в ПВЗ), utilization_date}.
        """
        out: List[Dict[str, Any]] = []
        last_id = ""
        page = min(max_total, 500)
        while True:
            payload = {
                "date_from": date_from,
                "date_to": date_to,
                "last_id": last_id,
                "limit": page,
            }
            data = await self._post("/v1/removal/from-stock/list", payload)
            chunk = data.get("returns_summary_report_rows") or []
            out.extend(chunk)
            last_id = data.get("last_id", "") or ""
            if not chunk or not last_id or len(out) >= max_total:
                break
        return out[:max_total]

    async def removal_from_supply_list(
        self,
        date_from: str,
        date_to: str,
        *,
        max_total: int = 5000,
    ) -> List[Dict[str, Any]]:
        """POST /v1/removal/from-supply/list — вывозы С ПОСТАВКИ.

        Товары которые Ozon отбраковал на приёмке поставки (битые, неправильно
        промаркированные и т.п.) и возвращает продавцу. В ЛК — раздел
        seller.ozon.ru/app/fbo-operations/returns?tab=Supply.

        Поля ответа идентичны removal_from_stock_list.
        """
        out: List[Dict[str, Any]] = []
        last_id = ""
        page = min(max_total, 500)
        while True:
            payload = {
                "date_from": date_from,
                "date_to": date_to,
                "last_id": last_id,
                "limit": page,
            }
            data = await self._post("/v1/removal/from-supply/list", payload)
            chunk = data.get("returns_summary_report_rows") or []
            out.extend(chunk)
            last_id = data.get("last_id", "") or ""
            if not chunk or not last_id or len(out) >= max_total:
                break
        return out[:max_total]

    async def supply_order_timeslot_update(
        self,
        supply_order_id: int,
        timeslot_from: str,
        timeslot_to: str,
    ) -> Dict[str, Any]:
        """POST /v1/supply-order/timeslot/update — обновить таймслот созданной поставки.

        Workaround по совету Ozon SS: при 429 на /v1/draft/timeslot/info можно создать
        поставку и затем выставить таймслот через этот endpoint.
        """
        payload = {
            "supply_order_id": supply_order_id,
            "timeslot": {
                "from_in_timezone": timeslot_from,
                "to_in_timezone": timeslot_to,
            },
        }
        return await self._post("/v1/supply-order/timeslot/update", payload)

    async def supply_order_list(
        self,
        *,
        states: Optional[List[str]] = None,
        sort_by: str = "ORDER_STATE_UPDATED_AT",
        sort_dir: str = "DESC",
        limit: int = 100,
        max_total: int = 1000,
    ) -> List[int]:
        """POST /v3/supply-order/list — список order_id с фильтром по статусу.

        states: ["REPORTS_CONFIRMATION_AWAITING", "REPORT_REJECTED", "COMPLETED",
        "DATA_FILLING", "READY_TO_SUPPLY", "ACCEPTED_AT_SUPPLY_WAREHOUSE",
        "IN_TRANSIT", "ACCEPTANCE_AT_STORAGE_WAREHOUSE", "REJECTED_AT_SUPPLY_WAREHOUSE",
        "CANCELLED", "OVERDUE"]. None/пусто = без фильтра.

        Возвращает [order_id, ...] (числа). Дальше через supply_order_get(order_ids)
        получить детали.
        """
        out: List[int] = []
        last_id = ""
        page = min(limit, 100)
        while True:
            payload: Dict[str, Any] = {
                "filter": {},
                "last_id": last_id,
                "limit": page,
                "sort_by": sort_by,
                "sort_dir": sort_dir,
            }
            if states:
                payload["filter"]["states"] = states
            data = await self._post("/v3/supply-order/list", payload)
            chunk = data.get("order_ids") or []
            for x in chunk:
                try:
                    out.append(int(x))
                except (ValueError, TypeError):
                    continue
            last_id = data.get("last_id", "") or ""
            if not chunk or not last_id or len(out) >= max_total:
                break
        return out[:max_total]

    async def supply_order_get(self, order_ids: List[int]) -> List[Dict[str, Any]]:
        """POST /v3/supply-order/get — детали по созданным поставкам (до 50 за раз).

        Возвращает список orders[] с полями: order_id, order_number, state,
        state_updated_date, timeslot, dropoff_warehouse, supplies, ...
        Если order_ids пуст — возвращает [].
        """
        if not order_ids:
            return []
        # Ozon принимает строки, не числа.
        payload = {"order_ids": [str(x) for x in order_ids[:50]]}
        resp = await self._post("/v3/supply-order/get", payload)
        return resp.get("orders") or []

    async def supply_order_bundle(self, bundle_ids: List[str]) -> List[Dict[str, Any]]:
        """POST /v1/supply-order/bundle — состав поставки по bundle_id.

        bundle_ids берутся из supplies[].bundle_id (от supply_order_get).
        Возвращает items[] с полями: sku, offer_id, name, quantity, barcode и т.п.
        Если bundle_ids пуст — возвращает [].
        """
        if not bundle_ids:
            return []
        payload = {"bundle_ids": [str(x) for x in bundle_ids]}
        resp = await self._post("/v1/supply-order/bundle", payload)
        return resp.get("items") or resp.get("bundle_items") or []

    async def supply_order_cancel(self, order_id: int) -> str:
        """POST /v1/supply-order/cancel — асинхронная отмена supply order.
        Возвращает operation_id для последующего poll'а через cancel/status."""
        resp = await self._post("/v1/supply-order/cancel", {"order_id": int(order_id)})
        return resp.get("operation_id") or ""

    async def supply_order_cancel_status(self, operation_id: str) -> Dict[str, Any]:
        """POST /v1/supply-order/cancel/status — проверить статус отмены.
        Возвращает {error_reasons, result: {is_order_cancelled, supplies: [...]}}.
        error_reasons enum: INVALID_ORDER_STATE | ORDER_IS_VIRTUAL |
        ORDER_DOES_NOT_BELONG_TO_CONTRACTOR | ORDER_DOES_NOT_BELONG_TO_COMPANY |
        OTHER_ASYNCHRONOUS_OPERATION_IN_PROGRESS."""
        return await self._post("/v1/supply-order/cancel/status", {"operation_id": operation_id})
