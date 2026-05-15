"""Команды для read-only интеграций с WB и Ozon.

/api_check            — проверить наличие ключей и связь
/wb_stocks            — остатки WB по складам
/wb_coefs             — коэффициенты приёмки WB по складам
/ozon_stocks          — остатки Ozon FBO
/ozon_warehouses      — список складов Ozon (FBO кластеры)
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Dict, List

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from src.bot.helpers import send_long
from src.config import APIKEY_OZON, CLIENT_ID_OZON, APIKEY_WB, OZON_PROXY_URL
from src.db.models import Sku
from src.db.session import db_session
from src.integrations import OzonClient, OzonAPIError, WBClient, WBAPIError

router = Router()
logger = logging.getLogger("bot.integrations")


# ── /ozon_diag — диагностический запрос на draft/create ─────────────────────

@router.message(Command("ozon_diag"))
async def cmd_ozon_diag(msg: Message) -> None:
    """Минимальный тест draft/create + полные headers ответа в чат.
    Покажет реальные rate-limit headers и текст ошибки для диагностики 429."""
    if not APIKEY_OZON or not CLIENT_ID_OZON:
        await msg.answer("⚠ Нет Ozon-ключей.")
        return

    import httpx
    from src.integrations.ozon_api import OZON_BASE

    headers = {
        "Client-Id": CLIENT_ID_OZON,
        "Api-Key": APIKEY_OZON,
        "Content-Type": "application/json",
    }
    # Минимальный payload — Ozon должен либо принять (вернуть op_id), либо
    # ругнуться валидацией (400), либо отдать 429 с headers
    payload = {
        "items": [{"offer_id": "TEST-NONEXISTENT", "quantity": 1}],
        "type": "CREATE_TYPE_CROSSDOCK",
    }

    await msg.answer("🔬 Ozon diag: POST /v1/draft/create с минимальным payload…")

    async with httpx.AsyncClient(timeout=20.0) as cli:
        try:
            r = await cli.post(f"{OZON_BASE}/v1/draft/create", headers=headers, json=payload)
        except Exception as e:
            await msg.answer(f"❌ {type(e).__name__}: <code>{str(e)[:200]}</code>")
            return

    # Все заголовки которые могут содержать rate-limit инфу
    rl_keys = [k for k in r.headers if any(p in k.lower() for p in ["limit", "retry", "rate", "request"])]
    rl_dump = "\n".join(f"  <code>{k}</code>: {r.headers.get(k)}" for k in rl_keys)
    if not rl_dump:
        rl_dump = "  (нет rate-limit headers)"

    body = (r.text or "")[:600]

    out = (
        f"📡 <b>Ozon /v1/draft/create — диагностика</b>\n\n"
        f"<b>Status</b>: <code>{r.status_code}</code>\n\n"
        f"<b>Rate-limit headers:</b>\n{rl_dump}\n\n"
        f"<b>Body:</b>\n<code>{body}</code>"
    )
    await send_long(msg, out)


# ── /api_warmup — прогреть кэш WB (файловый) ────────────────────────────────

@router.message(Command("api_warmup"))
async def cmd_api_warmup(msg: Message) -> None:
    """Запросить и закэшировать в файл WB-склады и Ozon-кластеры.
    Полезно прогнать после старта бота — потом /ship_hunt не упадёт на 429."""
    import asyncio as _a
    lines = ["🔥 <b>Прогрев кэшей</b>\n"]

    if APIKEY_WB:
        try:
            wb = WBClient(APIKEY_WB)
            whs = await wb.warehouses()
            lines.append(f"  WB warehouses: ✅ {len(whs)} (закэшировано на 24ч)")
            # пауза перед след. запросом чтобы не упереться
            await _a.sleep(2)
            coefs = await wb.acceptance_coefficients()
            lines.append(f"  WB coefficients: ✅ {len(coefs)} (in-memory 90с)")
        except WBAPIError as e:
            lines.append(f"  WB: ⚠ <code>{str(e)[:200]}</code>")
        except Exception as e:
            lines.append(f"  WB: ❌ {type(e).__name__}: <code>{str(e)[:150]}</code>")
    else:
        lines.append("  WB: ⏭ нет APIKEY_WB")

    if APIKEY_OZON and CLIENT_ID_OZON:
        try:
            oz = OzonClient(CLIENT_ID_OZON, APIKEY_OZON, proxy=OZON_PROXY_URL)
            cl = await oz.cluster_list()
            lines.append(f"  Ozon clusters: ✅ {len(cl)}")
        except Exception as e:
            lines.append(f"  Ozon: ❌ {type(e).__name__}: <code>{str(e)[:150]}</code>")
    else:
        lines.append("  Ozon: ⏭ нет ключей")

    await msg.answer("\n".join(lines))


# ── /api_check ──────────────────────────────────────────────────────────────

# Список тестовых методов для проверки доступа к Ozon Seller API.
# Группировка соответствует ролям API-ключа Ozon (см. OZON_API_USAGE.md).
# Каждый метод — read-only / безопасный (limit=1 / небольшое окно дат).
def _ozon_api_tests(cli: "OzonClient") -> list[tuple[str, str, callable]]:
    """Возвращает (роль, endpoint, async_callable). callable вызывает
    минимальный безопасный запрос к endpoint'у."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    iso_from = (now - timedelta(days=2)).strftime("%Y-%m-%dT00:00:00.000Z")
    iso_to = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    date_from = (now - timedelta(days=2)).strftime("%Y-%m-%d")
    date_to = now.strftime("%Y-%m-%d")
    return [
        ("Product read-only", "/v3/product/list",
         lambda: cli.product_list(limit=1)),
        ("Product read-only", "/v4/product/info/stocks",
         lambda: cli.stocks_fbo(limit=1)),
        ("Posting FBO", "/v3/posting/fbo/list",
         lambda: cli.postings_fbo_list(iso_from, iso_to, max_total=1)),
        ("Returns / Returns read-only", "/v1/returns/list",
         lambda: cli.returns_list(limit=1)),
        ("Returns", "/v1/return/giveout/is-enabled",
         lambda: cli.returns_giveout_is_enabled()),
        ("Returns / Returns read-only", "/v1/return/giveout/list",
         lambda: cli.returns_giveout_list(limit=1)),
        ("Report", "/v1/removal/from-stock/list",
         lambda: cli.removal_from_stock_list(date_from, date_to, max_total=1)),
        ("Report", "/v1/removal/from-supply/list",
         lambda: cli.removal_from_supply_list(date_from, date_to, max_total=1)),
        ("Supply order / Warehouse", "/v1/cluster/list",
         lambda: cli.cluster_list()),
        ("Warehouse", "/v1/warehouse/fbs/create/drop-off/list",
         lambda: cli.warehouse_fbs_drop_off_list(address_search="Москва")),
    ]


def _classify_ozon_error(e: Exception) -> tuple[str, str]:
    """Возвращает (icon, hint). Icon: ✅ ⚠ ❌. Hint: короткий комментарий."""
    s = str(e)
    low = s.lower()
    if "401" in s or "unauthorized" in low:
        return ("❌", "401 — ключ невалиден")
    if "403" in s or "forbidden" in low or "access_denied" in low:
        return ("❌", "403 — нет роли в ключе")
    if "404" in s and "not found" not in low.split("404", 1)[0][-20:]:
        # 404 на read-list endpoint'ах = метод не существует / не даёт права
        return ("❌", "404 — метод не доступен")
    if "429" in s:
        return ("⚠", "429 — rate-limit, повтори через минуту")
    if "timeout" in low or "connect" in low:
        return ("⚠", "сеть/прокси не отвечает")
    return ("⚠", s[:80])


@router.message(Command("api_check"))
async def cmd_api_check(msg: Message) -> None:
    lines = ["🔑 <b>Проверка API-ключей</b>\n"]

    # ── Ozon ─────────────────────────────────────────────────────────────
    lines.append("<b>Ozon Seller API:</b>")
    lines.append(f"  CLIENT_ID: {'✅ ' + CLIENT_ID_OZON if CLIENT_ID_OZON else '❌ не задан (CLIEN_TID в .env)'}")
    lines.append(f"  API_KEY:   {'✅ ' + ('•' * min(len(APIKEY_OZON), 8)) + f' (len={len(APIKEY_OZON)})' if APIKEY_OZON else '❌ не задан (APIKEY_OZON)'}")
    lines.append("")

    if CLIENT_ID_OZON and APIKEY_OZON:
        await msg.answer("🔍 Тестирую Ozon endpoint'ы (10 вызовов)…")
        cli = OzonClient(CLIENT_ID_OZON, APIKEY_OZON, proxy=OZON_PROXY_URL)
        tests = _ozon_api_tests(cli)
        results: list[tuple[str, str, str, str]] = []  # (icon, role, endpoint, hint)
        for role, endpoint, call in tests:
            try:
                await call()
                results.append(("✅", role, endpoint, "OK"))
            except OzonAPIError as e:
                icon, hint = _classify_ozon_error(e)
                results.append((icon, role, endpoint, hint))
            except Exception as e:
                results.append(("❌", role, endpoint, f"{type(e).__name__}: {str(e)[:60]}"))

        # Группируем по ролям для красивого вывода.
        from collections import defaultdict
        by_role: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
        for icon, role, endpoint, hint in results:
            by_role[role].append((icon, endpoint, hint))

        ok_count = sum(1 for r in results if r[0] == "✅")
        fail_count = sum(1 for r in results if r[0] == "❌")
        warn_count = len(results) - ok_count - fail_count

        lines.append(f"<b>Доступ к методам:</b> ✅ {ok_count} · ❌ {fail_count} · ⚠ {warn_count}\n")
        for role, items in by_role.items():
            lines.append(f"<b>{role}</b>")
            for icon, endpoint, hint in items:
                tail = f" — <i>{hint}</i>" if hint and hint != "OK" else ""
                lines.append(f"  {icon} <code>{endpoint}</code>{tail}")
            lines.append("")

        # Эндпоинты, которые не тестируем (нужны для создания поставок,
        # требуют draft_id / order_ids и rate-limited глобально). Только
        # информативно.
        lines.append("<b>Не проверяются (нужны для создания поставок):</b>")
        for ep in (
            "/v1/draft/{direct,crossdock,multi-cluster}/create",
            "/v2/draft/create/info",
            "/v2/draft/timeslot/info  ⚠ только в роли Admin",
            "/v2/draft/supply/create  ⚠ только в роли Admin",
            "/v3/supply-order/get",
            "/v1/supply-order/cancel",
        ):
            lines.append(f"  ℹ <code>{ep}</code>")
        lines.append("")
        if fail_count:
            lines.append(
                "💡 Если есть ❌ — пересоздай ключ в Ozon ЛК с нужными ролями. "
                "Минимум для бота: <b>Admin</b> (или Product read-only + Posting FBO + "
                "Returns + Warehouse + Supply order + Report). См. OZON_API_USAGE.md."
            )

    lines.append("")

    # ── WB ───────────────────────────────────────────────────────────────
    lines.append("<b>Wildberries API:</b>")
    lines.append(f"  API_KEY: {'✅ ' + ('•' * 8) + f' (len={len(APIKEY_WB)})' if APIKEY_WB else '❌ не задан (APIKEY_WB)'}")

    if APIKEY_WB:
        try:
            cli = WBClient(APIKEY_WB)
            whs = await cli.warehouses()
            lines.append(f"  ✅ /api/v1/warehouses → {len(whs)} складов")
        except WBAPIError as e:
            lines.append(f"  ⚠ <code>{str(e)[:200]}</code>")
        except Exception as e:
            lines.append(f"  ❌ {type(e).__name__}: <code>{str(e)[:200]}</code>")

    await send_long(msg, "\n".join(lines))


# ── WB ──────────────────────────────────────────────────────────────────────

_WB_STOCKS_CACHE: Dict[str, tuple] = {}  # df → (ts, rows)


@router.message(Command("wb_stocks"))
async def cmd_wb_stocks(msg: Message) -> None:
    if not APIKEY_WB:
        await msg.answer("⚠ APIKEY_WB не задан в .env. Сначала /api_check.")
        return
    import time as _t
    cli = WBClient(APIKEY_WB)
    df = (date.today() - timedelta(days=1)).isoformat()

    # Кэш на 90 сек: WB лимитирует /supplier/stocks до ~1 req/min
    cache = _WB_STOCKS_CACHE.get(df)
    rows = None
    if cache and (_t.time() - cache[0]) < 90:
        rows = cache[1]
        await msg.answer(f"📦 WB остатки от {df} (из кэша, {int(_t.time() - cache[0])} сек назад)")
    else:
        await msg.answer(f"📡 WB: запрашиваю остатки от {df}…")
        try:
            rows = await cli.stocks(df)
            _WB_STOCKS_CACHE[df] = (_t.time(), rows)
        except WBAPIError as e:
            await msg.answer(
                f"⚠ WB API: <code>{str(e)[:500]}</code>\n\n"
                f"Если 429 — WB лимитирует /supplier/stocks ~1 раз в минуту, попробуй через 60 сек."
            )
            return
        except Exception as e:
            await msg.answer(f"❌ {type(e).__name__}: <code>{str(e)[:300]}</code>")
            return

    if not rows:
        await msg.answer("Остатков не получено.")
        return

    by_warehouse: Dict[str, int] = {}
    by_article: Dict[str, int] = {}
    for r in rows:
        wh = r.get("warehouseName") or "?"
        art = r.get("supplierArticle") or r.get("nmId") or "?"
        qty = int(r.get("quantity") or 0)
        by_warehouse[wh] = by_warehouse.get(wh, 0) + qty
        by_article[str(art)] = by_article.get(str(art), 0) + qty

    lines = [f"📦 WB остатки: {len(rows)} строк, {sum(by_warehouse.values())} шт всего\n"]
    lines.append("<b>По складам:</b>")
    for wh, q in sorted(by_warehouse.items(), key=lambda x: -x[1]):
        lines.append(f"  {wh[:40]} — <b>{q}</b>")
    lines.append(f"\n<b>По артикулам ({len(by_article)}):</b>")
    for a, q in sorted(by_article.items(), key=lambda x: -x[1]):
        lines.append(f"  <code>{a}</code> — <b>{q}</b>")
    await send_long(msg, "\n".join(lines))


def _f(v) -> float:
    try:
        return float(v) if v not in (None, "") else 0.0
    except (ValueError, TypeError):
        return 0.0


def _logistics_emoji(dlv_coef_pct: float) -> str:
    """≤120% зелёный, ≤150% жёлтый, иначе красный."""
    if dlv_coef_pct <= 120:
        return "🟢"
    if dlv_coef_pct <= 150:
        return "🟡"
    return "🔴"


@router.message(Command("wb_coefs"))
async def cmd_wb_coefs(msg: Message) -> None:
    from src.warehouses import WB_CLUSTERS, WB_FOOD_WAREHOUSES
    if not APIKEY_WB:
        await msg.answer("⚠ APIKEY_WB не задан в .env. Сначала /api_check.")
        return
    cli = WBClient(APIKEY_WB)
    await msg.answer("📡 WB: коэффициенты приёмки + логистика…")
    try:
        rows = await cli.acceptance_coefficients()
    except WBAPIError as e:
        await msg.answer(
            f"⚠ WB API: <code>{str(e)[:500]}</code>\n\n"
            f"Проверь что у токена есть скоуп <b>«Поставки»</b>. /api_check для проверки."
        )
        return
    except Exception as e:
        await msg.answer(f"❌ {type(e).__name__}: <code>{str(e)[:300]}</code>")
        return

    if not rows:
        await msg.answer("Данных нет.")
        return

    food_set = set(WB_FOOD_WAREHOUSES)
    # Берём по складу лучшую строку: сначала по логистике (дешевле), потом по приёмке
    best_by_wh: Dict[str, Dict] = {}
    for r in rows:
        wh = r.get("warehouseName") or ""
        if wh not in food_set:
            continue
        coef = r.get("coefficient")
        if coef is None or coef < 0:
            continue
        dlv_coef = _f(r.get("deliveryCoef"))
        cur = best_by_wh.get(wh)
        better = (
            cur is None
            or dlv_coef < cur["dlv_coef"]
            or (dlv_coef == cur["dlv_coef"] and coef < cur["coefficient"])
        )
        if better:
            best_by_wh[wh] = {"coefficient": coef, "dlv_coef": dlv_coef}

    if not best_by_wh:
        await msg.answer("Нет доступных слотов по продуктовым складам.")
        return

    # Группируем по кластерам
    lines = [f"📊 <b>WB: приёмка + логистика</b> ({len(best_by_wh)} складов)\n"]
    for cluster_name, wh_list in WB_CLUSTERS.items():
        cluster_rows = [(wh, best_by_wh[wh]) for wh in wh_list if wh in best_by_wh]
        if not cluster_rows:
            continue
        # Внутри кластера — сначала дешевле по логистике
        cluster_rows.sort(key=lambda x: (x[1]["dlv_coef"], x[1]["coefficient"]))
        lines.append(f"\n🏭 <b>{cluster_name}</b>")
        for wh, info in cluster_rows:
            pct = info["dlv_coef"]  # API возвращает уже в процентах (например 125 = 125%)
            emoji = _logistics_emoji(pct)
            lines.append(f"{emoji} {wh} — <b>{pct:g}%</b> логистика, <b>×{info['coefficient']}</b> приёмка")

    # Склады продуктовые без данных (всё -1)
    missing = [w for w in WB_FOOD_WAREHOUSES if w not in best_by_wh]
    if missing:
        lines.append(f"\n⛔ Недоступны сейчас ({len(missing)}): {', '.join(missing[:8])}"
                     + (f" …и ещё {len(missing) - 8}" if len(missing) > 8 else ""))

    await send_long(msg, "\n".join(lines))


# ── Ozon ────────────────────────────────────────────────────────────────────

@router.message(Command("ozon_stocks"))
async def cmd_ozon_stocks(msg: Message) -> None:
    if not APIKEY_OZON or not CLIENT_ID_OZON:
        await msg.answer("⚠ APIKEY_OZON или CLIEN_TID не заданы в .env. /api_check для проверки.")
        return
    cli = OzonClient(CLIENT_ID_OZON, APIKEY_OZON, proxy=OZON_PROXY_URL)
    await msg.answer("📡 Ozon: остатки FBO (все артикулы)…")
    try:
        items = await cli.stocks_fbo(limit=5000)
    except OzonAPIError as e:
        await msg.answer(f"⚠ Ozon API: <code>{str(e)[:500]}</code>")
        return
    except Exception as e:
        await msg.answer(f"❌ {type(e).__name__}: <code>{str(e)[:300]}</code>")
        return

    if not items:
        await msg.answer("Остатков не получено.")
        return

    by_article: Dict[str, int] = {}
    for it in items:
        art = it.get("offer_id") or str(it.get("product_id", "?"))
        present = sum(int(s.get("present") or 0) for s in (it.get("stocks") or []))
        by_article[art] = by_article.get(art, 0) + present

    total = sum(by_article.values())
    nonzero = {a: q for a, q in by_article.items() if q > 0}
    lines = [f"📦 Ozon остатки: {len(items)} SKU, {total} шт всего, {len(nonzero)} с ненулевым\n"]
    lines.append(f"<b>Все артикулы с остатком (отсортировано):</b>")
    for a, q in sorted(nonzero.items(), key=lambda x: -x[1]):
        lines.append(f"  <code>{a}</code> — <b>{q}</b>")
    await send_long(msg, "\n".join(lines))


# ── импорт каталога: snapshot Ozon/WB catalog ──────────────────────────────

@router.message(Command("sku_link_ozon"))
async def cmd_sku_link_ozon(msg: Message) -> None:
    """Полный snapshot Ozon-каталога в локальную таблицу ozon_products.

    Стирает старые записи и записывает заново — никакого matching по barcode
    с локальным каталогом. ozon_products теперь источник правды для Ozon-флоу.
    """
    if not APIKEY_OZON or not CLIENT_ID_OZON:
        await msg.answer("⚠ Ozon-ключи не заданы. /api_check.")
        return
    cli = OzonClient(CLIENT_ID_OZON, APIKEY_OZON, proxy=OZON_PROXY_URL)
    await msg.answer("📡 Ozon: тяну весь каталог…")

    try:
        prods = await cli.product_list(limit=5000)
        if not prods:
            await msg.answer("Каталог Ozon пуст.")
            return
        ids = [p.get("product_id") for p in prods if p.get("product_id")]
        await msg.answer(f"Получил {len(ids)} товаров. Запрашиваю детали (с barcode)…")
        infos = await cli.product_info_list(ids)
    except OzonAPIError as e:
        await msg.answer(f"⚠ Ozon API: <code>{str(e)[:500]}</code>")
        return

    from src.db.models import OzonProduct
    added = updated = 0
    with db_session() as session:
        existing = {p.offer_id: p for p in session.query(OzonProduct).all()}
        seen_offers = set()
        for it in infos:
            offer_id = it.get("offer_id")
            if not offer_id:
                continue
            seen_offers.add(offer_id)
            ozon_sku = (
                it.get("sku") or it.get("product_id")
                or it.get("fbo_sku") or it.get("fbs_sku")
            )
            try:
                ozon_sku = int(ozon_sku) if ozon_sku else None
            except (ValueError, TypeError):
                ozon_sku = None
            name = it.get("name") or ""
            bcs = []
            if it.get("barcode"):
                bcs.append(str(it["barcode"]))
            for b in it.get("barcodes") or []:
                if b:
                    bcs.append(str(b))
            primary_bc = bcs[0] if bcs else None

            if offer_id in existing:
                p = existing[offer_id]
                p.sku = ozon_sku
                p.name = name[:256]
                p.barcode_primary = primary_bc
                p.raw_barcodes_json = bcs
                updated += 1
            else:
                session.add(OzonProduct(
                    offer_id=offer_id,
                    sku=ozon_sku,
                    name=name[:256],
                    barcode_primary=primary_bc,
                    raw_barcodes_json=bcs,
                ))
                added += 1

        # Удаляем те что есть локально но нет в актуальном каталоге
        stale = [p for offer, p in existing.items() if offer not in seen_offers]
        for p in stale:
            session.delete(p)

    lines = [
        f"✅ Каталог Ozon синхронизирован.",
        f"  • новых: {added}",
        f"  • обновлено: {updated}",
        f"  • удалено (нет в кабинете): {len(stale)}",
        f"Всего в Ozon: {len(prods)} товаров.",
    ]
    await send_long(msg, "\n".join(lines))


@router.message(Command("sku_link_wb"))
async def cmd_sku_link_wb(msg: Message) -> None:
    """Полный snapshot WB-каталога в локальную таблицу wb_products."""
    if not APIKEY_WB:
        await msg.answer("⚠ APIKEY_WB не задан. /api_check.")
        return
    cli = WBClient(APIKEY_WB)
    await msg.answer("📡 WB: тяну каталог карточек (Content API)…")
    try:
        cards = await cli.cards_list(limit_total=5000)
    except WBAPIError as e:
        await msg.answer(
            f"⚠ WB API: <code>{str(e)[:500]}</code>\n\n"
            f"Проверь скоуп токена «Контент». /api_check."
        )
        return

    if not cards:
        await msg.answer("Карточек нет.")
        return

    from src.db.models import WbProduct
    added = updated = 0
    with db_session() as session:
        existing = {p.nm_id: p for p in session.query(WbProduct).all()}
        seen_nms = set()
        for c in cards:
            nm = c.get("nmID")
            if not nm:
                continue
            nm = int(nm)
            seen_nms.add(nm)
            article = c.get("vendorCode") or ""
            name = c.get("title") or ""
            bcs = []
            for size in c.get("sizes") or []:
                for bc in size.get("skus") or []:
                    bcs.append(str(bc).strip())
            primary_bc = bcs[0] if bcs else None

            if nm in existing:
                p = existing[nm]
                p.article = article[:128] or None
                p.name = name[:256] or None
                p.barcode_primary = primary_bc
                p.raw_barcodes_json = bcs
                updated += 1
            else:
                session.add(WbProduct(
                    nm_id=nm,
                    article=article[:128] or None,
                    name=name[:256] or None,
                    barcode_primary=primary_bc,
                    raw_barcodes_json=bcs,
                ))
                added += 1
        # Удаляем те что нет в актуальном каталоге
        stale = [p for nm, p in existing.items() if nm not in seen_nms]
        for p in stale:
            session.delete(p)

    lines = [
        f"✅ Каталог WB синхронизирован.",
        f"  • новых: {added}",
        f"  • обновлено: {updated}",
        f"  • удалено (нет в кабинете): {len(stale)}",
        f"Всего в WB: {len(cards)} карточек.",
    ]
    await send_long(msg, "\n".join(lines))


@router.message(Command("ozon_warehouses"))
async def cmd_ozon_warehouses(msg: Message) -> None:
    if not APIKEY_OZON or not CLIENT_ID_OZON:
        await msg.answer("⚠ APIKEY_OZON или CLIEN_TID не заданы в .env. /api_check.")
        return
    cli = OzonClient(CLIENT_ID_OZON, APIKEY_OZON, proxy=OZON_PROXY_URL)
    await msg.answer("📡 Ozon: кластеры FBO…")
    try:
        clusters = await cli.cluster_list()
    except OzonAPIError as e:
        await msg.answer(f"⚠ Ozon API: <code>{str(e)[:500]}</code>")
        return
    except Exception as e:
        await msg.answer(f"❌ {type(e).__name__}: <code>{str(e)[:300]}</code>")
        return

    if not clusters:
        await msg.answer("Кластеров не получено.")
        return

    lines = [f"🏭 Ozon FBO: {len(clusters)} кластеров\n"]
    for cl in clusters:
        cname = cl.get("name") or cl.get("id") or "?"
        warehouses = []
        for lc in cl.get("logistic_clusters") or []:
            for w in lc.get("warehouses") or []:
                wn = w.get("name") or w.get("warehouse_id") or "?"
                warehouses.append(str(wn))
        lines.append(f"<b>{cname}</b> ({len(warehouses)} скл.)")
        for w in warehouses:
            lines.append(f"  • {w}")
        lines.append("")
    await send_long(msg, "\n".join(lines))
