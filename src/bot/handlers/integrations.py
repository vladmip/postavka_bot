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
from src.config import OZON_PROXY_URL
from src.db.models import Sku
from src.db.session import db_session
from src.integrations import OzonClient, OzonAPIError, WBClient, WBAPIError
from src.services.user_service import (
    current_user_id_from,
    get_ozon_client_for,
    get_ozon_creds,
    get_wb_api_key,
)


_NEED_OZON = (
    "⚠ Сначала добавь Ozon-ключи: /start → «Добавить Ozon»."
)
_NEED_WB = (
    "⚠ Сначала добавь WB-ключ: /start → «Добавить WB»."
)

router = Router()
logger = logging.getLogger("bot.integrations")


# ── /ozon_diag — диагностический запрос на draft/create ─────────────────────

@router.message(Command("ozon_diag"))
async def cmd_ozon_diag(msg: Message, _tg_id: int | None = None) -> None:
    """Минимальный тест draft/create + полные headers ответа в чат.
    Покажет реальные rate-limit headers и текст ошибки для диагностики 429.
    `_tg_id` — explicit override для случая когда команда вызывается из
    callback (cb.message.from_user — это бот, не юзер)."""
    tg_id = _tg_id if _tg_id is not None else current_user_id_from(msg)
    if tg_id is None:
        return
    with db_session() as s:
        creds = get_ozon_creds(s, tg_id)
    if creds is None:
        await msg.answer(_NEED_OZON)
        return

    import httpx
    from src.integrations.ozon_api import OZON_BASE

    headers = {
        "Client-Id": creds.client_id,
        "Api-Key": creds.api_key,
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
async def cmd_api_warmup(msg: Message, _tg_id: int | None = None) -> None:
    """Запросить и закэшировать в файл WB-склады и Ozon-кластеры.
    Полезно прогнать после старта бота — потом /ship_hunt не упадёт на 429."""
    import asyncio as _a
    lines = ["🔥 <b>Прогрев кэшей</b>\n"]
    tg_id = _tg_id if _tg_id is not None else current_user_id_from(msg)
    if tg_id is None:
        return

    with db_session() as s:
        wb_key = get_wb_api_key(s, tg_id)
        oz = get_ozon_client_for(s, tg_id)

    if wb_key:
        try:
            wb = WBClient(wb_key)
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
        lines.append("  WB: ⏭ нет ключа")

    if oz is not None:
        try:
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
async def cmd_api_check(msg: Message, _tg_id: int | None = None) -> None:
    import asyncio
    lines = ["🔑 <b>Проверка API-ключей</b>\n"]
    tg_id = _tg_id if _tg_id is not None else current_user_id_from(msg)
    if tg_id is None:
        return

    with db_session() as s:
        oz_creds = get_ozon_creds(s, tg_id)
        wb_key = get_wb_api_key(s, tg_id)

    # ── Ozon ─────────────────────────────────────────────────────────────
    lines.append("<b>Ozon Seller API:</b>")
    lines.append(f"  CLIENT_ID: {'✅ задан' if oz_creds else '❌ не задан (/start → Ozon)'}")
    lines.append(f"  API_KEY:   {'✅ задан' if oz_creds else '❌ не задан (/start → Ozon)'}")
    lines.append("")

    if oz_creds:
        placeholder = await msg.answer("🔍 Тестирую Ozon endpoint'ы…")
        cli = OzonClient(oz_creds.client_id, oz_creds.api_key, proxy=OZON_PROXY_URL)
        tests = _ozon_api_tests(cli)

        # Параллелим запросы (semaphore=4 чтобы не упереться в rate-limit Ozon).
        sem = asyncio.Semaphore(4)

        async def _run(role: str, endpoint: str, call) -> tuple[str, str, str, str]:
            async with sem:
                try:
                    await call()
                    return ("✅", role, endpoint, "OK")
                except OzonAPIError as e:
                    icon, hint = _classify_ozon_error(e)
                    return (icon, role, endpoint, hint)
                except Exception as e:
                    return ("❌", role, endpoint, f"{type(e).__name__}: {str(e)[:60]}")

        results = await asyncio.gather(*[_run(r, e, c) for r, e, c in tests])

        # Удаляем placeholder, чтобы не плодить сообщения.
        try:
            await placeholder.delete()
        except Exception:
            pass

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

        lines.append(
            "ℹ <i>Не проверяются (нужны Admin для создания поставок): "
            "<code>/v2/draft/timeslot/info</code>, <code>/v2/draft/supply/create</code>, "
            "<code>/v1/draft/*/create</code>, <code>/v3/supply-order/get</code>.</i>"
        )
        if fail_count:
            lines.append(
                "\n💡 Есть ❌ — пересоздай ключ в Ozon ЛК с нужными ролями. "
                "Минимум: <b>Admin</b> (либо Product read-only + Posting FBO + "
                "Returns + Warehouse + Supply order + Report)."
            )

    lines.append("")

    # ── WB ───────────────────────────────────────────────────────────────
    lines.append("<b>Wildberries API:</b>")
    lines.append(f"  API_KEY: {'✅ задан' if wb_key else '❌ не задан (/start → WB)'}")

    if wb_key:
        try:
            cli = WBClient(wb_key)
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
async def cmd_wb_stocks(msg: Message, _tg_id: int | None = None) -> None:
    tg_id = _tg_id if _tg_id is not None else current_user_id_from(msg)
    if tg_id is None:
        return
    with db_session() as s:
        wb_key = get_wb_api_key(s, tg_id)
    if not wb_key:
        await msg.answer(_NEED_WB)
        return
    import time as _t
    cli = WBClient(wb_key)
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
async def cmd_wb_coefs(msg: Message, _tg_id: int | None = None) -> None:
    from src.warehouses import WB_CLUSTERS, WB_FOOD_WAREHOUSES
    tg_id = _tg_id if _tg_id is not None else current_user_id_from(msg)
    if tg_id is None:
        return
    with db_session() as s:
        wb_key = get_wb_api_key(s, tg_id)
    if not wb_key:
        await msg.answer(_NEED_WB)
        return
    cli = WBClient(wb_key)
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
async def cmd_ozon_stocks(msg: Message, _tg_id: int | None = None) -> None:
    tg_id = _tg_id if _tg_id is not None else current_user_id_from(msg)
    if tg_id is None:
        return
    with db_session() as s:
        cli = get_ozon_client_for(s, tg_id)
    if cli is None:
        await msg.answer(_NEED_OZON)
        return
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
async def cmd_sku_link_ozon(msg: Message, _tg_id: int | None = None) -> None:
    """Полный snapshot Ozon-каталога в локальную таблицу ozon_products.

    Стирает старые записи и записывает заново — никакого matching по barcode
    с локальным каталогом. ozon_products теперь источник правды для Ozon-флоу.
    `_tg_id` — explicit override (callback-обёртки передают cb.from_user.id)."""
    tg_id = _tg_id if _tg_id is not None else current_user_id_from(msg)
    if tg_id is None:
        return
    from src.config import ALLOWED_USER_ID
    with db_session() as s:
        cli = get_ozon_client_for(s, tg_id)
    if cli is None:
        await msg.answer(_NEED_OZON)
        return
    # Одно сообщение, обновляем edit_text на каждом шаге — не плодим N сообщений
    # как раньше (см. feedback_max_edit).
    placeholder = await msg.answer("📡 <b>Ozon: тяну каталог…</b>")

    async def _edit(text: str) -> None:
        try:
            await placeholder.edit_text(text)
        except Exception:
            pass  # MessageNotModified и т.п.

    try:
        prods = await cli.product_list(limit=5000)
        if not prods:
            await _edit("ℹ Каталог Ozon пуст.")
            return
        ids = [p.get("product_id") for p in prods if p.get("product_id")]
        await _edit(
            f"📡 <b>Ozon: каталог получен</b>\n"
            f"  • товаров: {len(ids)}\n"
            f"  • запрашиваю детали (barcode)…"
        )
        infos = await cli.product_info_list(ids)
    except OzonAPIError as e:
        await _edit(f"⚠ Ozon API: <code>{str(e)[:500]}</code>")
        return

    from sqlalchemy import or_
    from src.db.models import OzonProduct
    added = updated = 0
    with db_session() as session:
        # Берём ТОЛЬКО свои записи (+ legacy с user_id=None для Vladislav).
        q = session.query(OzonProduct).filter(OzonProduct.user_id == tg_id)
        if tg_id == ALLOWED_USER_ID:
            q = session.query(OzonProduct).filter(
                or_(OzonProduct.user_id == tg_id, OzonProduct.user_id.is_(None))
            )
        existing = {p.offer_id: p for p in q.all()}
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
                # legacy-запись (user_id=None) — присвоим текущему юзеру.
                if p.user_id is None:
                    p.user_id = tg_id
                p.sku = ozon_sku
                p.name = name[:256]
                p.barcode_primary = primary_bc
                p.raw_barcodes_json = bcs
                updated += 1
            else:
                session.add(OzonProduct(
                    user_id=tg_id,
                    offer_id=offer_id,
                    sku=ozon_sku,
                    name=name[:256],
                    barcode_primary=primary_bc,
                    raw_barcodes_json=bcs,
                ))
                added += 1

        # Удаляем СВОИ записи, отсутствующие в актуальном каталоге.
        stale = [p for offer, p in existing.items() if offer not in seen_offers]
        for p in stale:
            session.delete(p)

    lines = [
        f"✅ <b>Каталог Ozon синхронизирован</b>",
        f"  • новых: {added}",
        f"  • обновлено: {updated}",
        f"  • удалено (нет в кабинете): {len(stale)}",
        f"  • всего в Ozon: <b>{len(prods)}</b>",
    ]
    await _edit("\n".join(lines))


@router.message(Command("sku_link_wb"))
async def cmd_sku_link_wb(msg: Message, _tg_id: int | None = None) -> None:
    """Полный snapshot WB-каталога в локальную таблицу wb_products."""
    tg_id = _tg_id if _tg_id is not None else current_user_id_from(msg)
    if tg_id is None:
        return
    from src.config import ALLOWED_USER_ID
    with db_session() as s:
        wb_key = get_wb_api_key(s, tg_id)
    if not wb_key:
        await msg.answer(_NEED_WB)
        return
    cli = WBClient(wb_key)
    placeholder = await msg.answer("📡 <b>WB: тяну каталог карточек…</b>")

    async def _edit(text: str) -> None:
        try:
            await placeholder.edit_text(text)
        except Exception:
            pass

    try:
        cards = await cli.cards_list(limit_total=5000)
    except WBAPIError as e:
        await _edit(
            f"⚠ WB API: <code>{str(e)[:500]}</code>\n\n"
            f"Проверь скоуп токена «Контент». /api_check."
        )
        return

    if not cards:
        await _edit("ℹ Карточек WB нет.")
        return

    from sqlalchemy import or_
    from src.db.models import WbProduct
    added = updated = 0
    with db_session() as session:
        q = session.query(WbProduct).filter(WbProduct.user_id == tg_id)
        if tg_id == ALLOWED_USER_ID:
            q = session.query(WbProduct).filter(
                or_(WbProduct.user_id == tg_id, WbProduct.user_id.is_(None))
            )
        existing = {p.nm_id: p for p in q.all()}
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
                if p.user_id is None:
                    p.user_id = tg_id
                p.article = article[:128] or None
                p.name = name[:256] or None
                p.barcode_primary = primary_bc
                p.raw_barcodes_json = bcs
                updated += 1
            else:
                session.add(WbProduct(
                    user_id=tg_id,
                    nm_id=nm,
                    article=article[:128] or None,
                    name=name[:256] or None,
                    barcode_primary=primary_bc,
                    raw_barcodes_json=bcs,
                ))
                added += 1
        stale = [p for nm, p in existing.items() if nm not in seen_nms]
        for p in stale:
            session.delete(p)

    lines = [
        f"✅ <b>Каталог WB синхронизирован</b>",
        f"  • новых: {added}",
        f"  • обновлено: {updated}",
        f"  • удалено (нет в кабинете): {len(stale)}",
        f"  • всего в WB: <b>{len(cards)}</b>",
    ]
    await _edit("\n".join(lines))


@router.message(Command("ozon_warehouses"))
async def cmd_ozon_warehouses(msg: Message, _tg_id: int | None = None) -> None:
    tg_id = _tg_id if _tg_id is not None else current_user_id_from(msg)
    if tg_id is None:
        return
    with db_session() as s:
        cli = get_ozon_client_for(s, tg_id)
    if cli is None:
        await msg.answer(_NEED_OZON)
        return
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
