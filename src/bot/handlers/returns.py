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
from src.services.user_service import current_user_id_from, get_wb_api_key
from src.db.session import db_session
from src.integrations import OzonClient, WBClient
from src.integrations.ozon_api import OzonAPIError
from src.integrations.wb_api import WBAPIError
from src.services.user_service import get_ozon_client_for

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
    tg_id = cb.from_user.id if cb.from_user else None
    with db_session() as s:
        oz = get_ozon_client_for(s, tg_id) if tg_id else None
    if oz is None:
        await safe_edit_or_answer(
            cb.message,
            "⚠ Ozon-ключи не подключены — пройди /start.",
            reply_markup=_back_kb(),
        )
        return
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

    if not all_returns:
        await safe_edit_or_answer(cb.message, "ℹ Возвратов в Ozon нет.", reply_markup=_back_kb())
        return

    # Если PDF получен — PDF без клавиатуры (на document-сообщении edit_text
    # технически невозможен → back-нав фолбэчилась бы в answer() и плодила бы
    # дубли при «◀ К возвратам» / «🏠 Меню»). Поэтому кнопки уносим в
    # маленькое текст-сообщение под PDF — его уже можно перерисовывать edit'ом.
    # Если PDF нет — текст со списком + инструкцией нажать в ЛК.
    if pdf_attached:
        caption_lines = [f"📄 <b>Ozon — возвраты к получению: {len(actionable)}</b>"]
        for r in (fbo + fbs)[:8]:
            prod = r.get("product") or {}
            place = r.get("place") or {}
            name = (prod.get("name") or "?")[:35]
            sku = prod.get("offer_id") or prod.get("sku") or "?"
            wh = (place.get("name") or "?")[:25]
            caption_lines.append(f"• {name} [{sku}] → {wh}")
        if len(fbo) + len(fbs) > 8:
            caption_lines.append(f"…и ещё {len(fbo) + len(fbs) - 8}")
        caption = "\n".join(caption_lines)[:1020]  # Telegram caption лимит 1024

        # Удаляем сообщение «🔍 Тяну…», шлём PDF + nav-сообщение под ним.
        try:
            await cb.message.delete()
        except Exception:
            pass
        file = BufferedInputFile(pdf_bytes, filename="ozon_returns.pdf")
        await cb.message.answer_document(file, caption=caption)
        await cb.message.answer(
            "<i>PDF готов — скачай и приложи на ПВЗ.</i>",
            reply_markup=_back_kb(),
        )
        return

    # PDF недоступен — текстовый список + инструкция, в ТОМ ЖЕ сообщении
    # (через edit_text). send_long тут не нужен: список ограничен MAX_PER_SECTION.
    if not actionable:
        lines.append("✅ Забирать сейчас нечего.")
    else:
        lines.append(
            "\n📄 <b>PDF этикетки пока нет.</b>\n"
            "Чтобы её получить — нажми «Получить возвраты» в Ozon ЛК."
        )
    await safe_edit_or_answer(cb.message, "\n".join(lines), reply_markup=_back_kb())


@router.callback_query(F.data == "ret:wb")
async def cb_ret_wb(cb: CallbackQuery) -> None:
    """WB возвраты через Statistics API /api/v1/supplier/sales — там можно
    отфильтровать возвраты по saleID, начинающемуся с "R"."""
    await cb.answer("Запрашиваю WB…")
    if not cb.message:
        return
    tg_id = current_user_id_from(cb)
    wb_key = None
    if tg_id is not None:
        from src.db.session import db_session as _db
        with _db() as _s:
            wb_key = get_wb_api_key(_s, tg_id)
    if not wb_key:
        await safe_edit_or_answer(
            cb.message,
            "⚠ WB-ключ не настроен. Открой /start → «Добавить WB».",
            reply_markup=_back_kb(),
        )
        return

    from datetime import datetime, timedelta
    date_from = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")
    cli = WBClient(wb_key)
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
        "\n<i>⚠ Это финансовые рефанды (когда деньги вернулись клиенту). "
        "Возвраты «в пути на ПВЗ» — это покупательский флоу на www.wildberries.ru, "
        "WB не отдаёт его через Seller API. PDF этикетки тоже только из ЛК.</i>"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 WB ЛК → Возвраты",
                              url="https://www.wildberries.ru/lk/myorders/delivery")],
        [InlineKeyboardButton(text="◀ К возвратам", callback_data="menu:returns")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:home")],
    ])
    # Текст до 15 возвратов + хедер — гарантированно <3900 символов, edit_text
    # справится. send_long всегда answer() и плодил бы новое сообщение.
    await safe_edit_or_answer(cb.message, "\n".join(lines), reply_markup=kb)
