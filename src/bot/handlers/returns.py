"""Возвраты — выгрузка одной PDF на маркетплейс + сводка содержимого.

Ozon: /v1/return/giveout/is-enabled → /list → /info → /get-pdf.
WB:   пока заглушка — endpoint для FBW возвратов не подтверждён.
"""
from __future__ import annotations

import logging
from io import BytesIO

from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, BufferedInputFile

from src.bot.helpers import safe_edit_or_answer, send_long
from src.config import APIKEY_OZON, CLIENT_ID_OZON, APIKEY_WB, OZON_PROXY_URL
from src.integrations import OzonClient, WBClient
from src.integrations.ozon_api import OzonAPIError
from src.integrations.wb_api import WBAPIError

router = Router()
logger = logging.getLogger("bot.returns")


def _back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀ К возвратам", callback_data="menu:returns")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:home")],
    ])


@router.callback_query(F.data == "ret:ozon")
async def cb_ret_ozon(cb: CallbackQuery) -> None:
    """Тянем общий список возвратов (FBO + FBS) и PDF этикетку для FBS-партии,
    если такая активна."""
    await cb.answer("Запрашиваю Ozon…")
    if not cb.message:
        return
    if not APIKEY_OZON or not CLIENT_ID_OZON:
        await safe_edit_or_answer(cb.message, "⚠ Ozon-ключи не заданы.", reply_markup=_back_kb())
        return

    oz = OzonClient(CLIENT_ID_OZON, APIKEY_OZON, proxy=OZON_PROXY_URL)
    await safe_edit_or_answer(cb.message, "🔍 Тяну возвраты из Ozon (FBO + FBS)…")

    # 1. Общий список возвратов
    try:
        all_returns = await oz.returns_list(limit=500)
    except OzonAPIError as e:
        await safe_edit_or_answer(
            cb.message,
            f"❌ Ozon /v1/returns/list: <code>{str(e)[:300]}</code>",
            reply_markup=_back_kb(),
        )
        return

    # Категоризируем по тому, что мы можем сделать пользователю полезного:
    # - "actionable" — лежит в пункте выдачи, можно забрать
    # - "in_transit" — едет в ПВЗ
    # - "archived" — уже на складе Ozon / получено / утилизировано (по сути архив)
    def _bucket(r: dict) -> str:
        v = (r.get("visual") or {}).get("status") or {}
        sys_name = (v.get("sys_name") or "").lower()
        display = (v.get("display_name") or "").lower()
        if "arrivedatreturnplace" in sys_name or "пункт" in display:
            return "actionable"
        if any(x in sys_name for x in ("transit", "intransport", "movingtoreturn")):
            return "in_transit"
        # Всё остальное (Received, OnOzonWarehouse, Disposed, Utilized, и т.п.) — архив
        return "archived"

    actionable = [r for r in all_returns if _bucket(r) == "actionable"]
    fbo = [r for r in actionable if str(r.get("schema") or "").upper().startswith("FBO")]
    fbs = [r for r in actionable if str(r.get("schema") or "").upper().startswith("FBS")]

    lines: list = [
        f"📥 <b>Ozon — возвраты к получению: {len(actionable)}</b>\n"
    ]

    def _fmt_return(r: dict) -> str:
        prod = r.get("product") or {}
        place = r.get("place") or {}
        visual = (r.get("visual") or {}).get("status") or {}
        status = visual.get("display_name") or visual.get("sys_name") or "?"
        name = (prod.get("name") or "?")[:45]
        sku = prod.get("offer_id") or prod.get("sku") or "?"
        qty = prod.get("quantity") or 1
        pn = r.get("posting_number") or ""
        addr = (place.get("address") or "")[:55]
        return (
            f"• <b>{name}</b> ×{qty} [<code>{sku}</code>]\n"
            f"  📍 {place.get('name') or '?'} <i>{addr}</i>\n"
            f"  Статус: {status}{(' · ' + pn) if pn else ''}"
        )

    MAX_PER_SECTION = 7
    if not actionable:
        lines.append("✅ Сейчас забирать нечего.")
    else:
        if fbo:
            lines.append(f"🟦 <b>FBO ({len(fbo)}):</b>")
            for r in fbo[:MAX_PER_SECTION]:
                lines.append(_fmt_return(r))
            if len(fbo) > MAX_PER_SECTION:
                lines.append(f"  …и ещё {len(fbo) - MAX_PER_SECTION}")

        if fbs:
            lines.append(f"\n🟧 <b>FBS ({len(fbs)}):</b>")
            for r in fbs[:MAX_PER_SECTION]:
                lines.append(_fmt_return(r))
            if len(fbs) > MAX_PER_SECTION:
                lines.append(f"  …и ещё {len(fbs) - MAX_PER_SECTION}")

    # PDF этикетки — пробуем тянуть всегда, даже если Ozon формально говорит
    # что «партий нет». Иногда get-pdf отдаёт документ напрямую.
    pdf_attached = False
    pdf_bytes = b""
    try:
        pdf_bytes = await oz.returns_giveout_get_pdf()
        # Не требуем строго '%PDF'-магических байт — Ozon может отдать с BOM
        # или иной обёрткой. Просто проверяем, что это похоже на бинарь разумного размера.
        if pdf_bytes and len(pdf_bytes) > 500:
            pdf_attached = True
            logger.info("Ozon PDF ready to send: %d bytes, head=%r",
                        len(pdf_bytes), pdf_bytes[:8])
    except OzonAPIError as e:
        logger.info("get-pdf failed: %s", e)

    if not pdf_attached and actionable:
        lines.append(
            "\n📄 <b>PDF этикетки пока нет.</b>\n"
            "Чтобы её сгенерировать — открой Ozon ЛК → раздел возвратов и нажми "
            "кнопку <b>«Получить возвраты»</b> в правом верхнем углу. После этого "
            "возвращайся сюда — PDF приедет документом."
        )

    if not all_returns:
        lines = ["ℹ Возвратов в Ozon нет."]

    # Кнопка прямой ссылки на ЛК Ozon → возвраты (status=30 = «в пункте выдачи»)
    ozon_returns_url = (
        "https://seller.ozon.ru/app/returns/supply/common"
        "?filters=%7B%22returnNumber%22%3A%22%22%2C%22postingNumber%22%3A%22%22"
        "%2C%22returnSchema%22%3A%22all%22%2C%22productArticle%22%3A%22%22"
        "%2C%22sort%22%3A%7B%22columnType%22%3A%22state_change%22"
        "%2C%22sortType%22%3A%22descending%22%7D%2C%22filterBy%22%3A%7B%7D"
        "%2C%22place%22%3A%22%22%2C%22barcode%22%3A%22%22%7D&status=30"
    )
    kb_rows = [
        [InlineKeyboardButton(text="🌐 Ozon ЛК → «Получить возвраты»",
                              url=ozon_returns_url)],
        [InlineKeyboardButton(text="◀ К возвратам", callback_data="menu:returns")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:home")],
    ]
    full_text = "\n".join(lines)
    await send_long(cb.message, full_text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))

    if pdf_attached:
        file = BufferedInputFile(pdf_bytes, filename="ozon_returns_giveout.pdf")
        await cb.message.answer_document(file, caption="📄 Этикетка получения возвратов Ozon")


@router.callback_query(F.data == "ret:wb")
async def cb_ret_wb(cb: CallbackQuery) -> None:
    """WB возвраты через Statistics API /api/v1/supplier/sales — там можно
    отфильтровать возвраты по saleID, начинающемуся с "R"."""
    await cb.answer("Запрашиваю WB…")
    if not cb.message:
        return
    if not APIKEY_WB:
        await safe_edit_or_answer(cb.message, "⚠ APIKEY_WB не задан.", reply_markup=_back_kb())
        return

    from datetime import datetime, timedelta
    date_from = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")
    cli = WBClient(APIKEY_WB)
    await safe_edit_or_answer(
        cb.message,
        f"🔍 Тяну WB Statistics /api/v1/supplier/sales с {date_from}…\n"
        "<i>⚠ Endpoint лимитирован ~1 req/min, может занять до 60 сек.</i>"
    )

    try:
        sales = await cli.sales(date_from=date_from, flag=0)
    except WBAPIError as e:
        text = (
            f"❌ WB API не отдал: <code>{str(e)[:300]}</code>\n\n"
        )
        if "401" in str(e) or "403" in str(e):
            text += (
                "<i>Похоже на ограничение токена. Открой WB ЛК → Настройки → API → "
                "проверь что у токена есть категория «Аналитика» (Statistics).</i>"
            )
        else:
            text += "<i>Можешь попробовать ещё раз через минуту.</i>"
        await safe_edit_or_answer(cb.message, text, reply_markup=_back_kb())
        return

    # Возвраты — это записи где saleID начинается с "R" (refund).
    refunds = [s for s in sales if str(s.get("saleID") or "").upper().startswith("R")]

    lines = [
        f"📥 <b>Wildberries — возвраты за 30 дней: {len(refunds)}</b>",
        f"<i>(всего операций в окне: {len(sales)})</i>\n",
    ]
    if not refunds:
        lines.append("✅ Возвратов нет.")
    else:
        for s in refunds[:15]:
            name = (s.get("subject") or s.get("supplierArticle") or "?")[:40]
            qty = 1  # WB sales не отдаёт qty per row, всегда 1
            art = s.get("supplierArticle") or "?"
            wh = s.get("warehouseName") or "?"
            date_str = (s.get("date") or "")[:10]
            price = s.get("priceWithDisc") or s.get("forPay") or 0
            lines.append(
                f"• <b>{name}</b> [{art}]\n"
                f"  📍 {wh} · {date_str} · {price}₽"
            )
        if len(refunds) > 15:
            lines.append(f"  …и ещё {len(refunds) - 15}")

    lines.append(
        "\n📄 <b>PDF этикетки получения у WB через API не отдаётся.</b>\n"
        "Для «забрать товар со склада WB» — открой WB ЛК → Поставки → "
        "Возвраты, там кнопка «Создать заявку на вывоз».\n"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 WB ЛК → Возвраты",
                              url="https://www.wildberries.ru/lk/myorders/delivery")],
        [InlineKeyboardButton(text="◀ К возвратам", callback_data="menu:returns")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:home")],
    ])
    await send_long(cb.message, "\n".join(lines), reply_markup=kb)
