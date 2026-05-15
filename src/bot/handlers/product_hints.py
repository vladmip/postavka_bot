"""Загрузка xlsx с упаковкой/примечаниями для товаров Ozon → таблица ProductHint.

Flow:
1. Юзер тапает «📑 Данные о товарах» в Настройках → pre-upload экран со счётчиками
   (всего товаров в каталоге, с hint, без hint).
2. Тап «📤 Загрузить xlsx» → state ProductHints.awaiting_file, бот ждёт файл.
3. Юзер кидает xlsx → парсим → резолвим артикулы в ozon_product_id →
   показываем сводку «✅ N распознано / ⚠ M не нашлись» + кнопки 🔄 Upsert / 🚮 Заменить всё / ✖ Отмена.
4. Юзер выбирает стратегию → пишем в БД → финальный экран с обновлёнными счётчиками.

Упаковка и примечания подставляются в ТЗ Отгрузка через ship_tz.py.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import List

from aiogram import Router, F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    BufferedInputFile,
)
from aiogram.fsm.context import FSMContext

from src.bot.helpers import safe_edit_or_answer
from src.bot.states import ProductHints
from src.config import STORAGE_DIR
from src.db.session import db_session
from src.parsers.product_hints import parse_hints_xlsx
from src.services.product_hint_service import (
    get_catalog_stats, resolve_rows, apply_upsert, apply_replace, ResolvedRow,
)
from src.generators.hints_template import generate_hints_template

logger = logging.getLogger("handlers.product_hints")
router = Router()


# ── Pre-upload экран ─────────────────────────────────────────────────────


def _build_intro_text() -> str:
    with db_session() as s:
        st = get_catalog_stats(s)
    if st.total == 0:
        return (
            "📑 <b>Данные о товарах (Ozon)</b>\n\n"
            "⚠ Каталог Ozon пуст. Сначала прогони <b>🛠 Диагностика → Кластеры Ozon</b> "
            "или /refresh_ozon_catalog, чтобы подтянуть товары."
        )
    return (
        "📑 <b>Данные о товарах (Ozon)</b>\n\n"
        f"Всего товаров в каталоге: <b>{st.total}</b>\n"
        f"✅ С упаковкой/примечанием: <b>{st.with_hint}</b>\n"
        f"⚠ Без данных: <b>{st.without_hint}</b>\n\n"
        "Загрузить xlsx с колонками <b>артикул | упаковка | примечание</b> — "
        "бот обновит данные. На случай если что-то изменилось у части товаров — "
        "можно лить файл частями, в режиме Upsert старое не пропадёт."
    )


def _intro_kb(has_catalog: bool) -> InlineKeyboardMarkup:
    rows = []
    if has_catalog:
        rows.append([InlineKeyboardButton(
            text="📋 Скачать шаблон с моими артикулами", callback_data="phints:template",
        )])
        rows.append([InlineKeyboardButton(text="📤 Загрузить xlsx", callback_data="phints:upload")])
    rows.append([InlineKeyboardButton(text="◀ В настройки", callback_data="menu:settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "menu:product_hints")
async def cb_menu_open(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.clear()
    if not cb.message:
        return
    with db_session() as s:
        has_catalog = get_catalog_stats(s).total > 0
    await safe_edit_or_answer(cb.message, _build_intro_text(), reply_markup=_intro_kb(has_catalog))


@router.callback_query(F.data == "phints:template")
async def cb_template_download(cb: CallbackQuery) -> None:
    await cb.answer("Готовлю шаблон…")
    if not cb.message:
        return
    with db_session() as s:
        st = get_catalog_stats(s)
        if st.total == 0:
            await cb.message.answer(
                "⚠ В каталоге нет товаров Ozon. Сначала прогони /refresh_ozon_catalog."
            )
            return
        data = generate_hints_template(s)
    fname = "Шаблон_упаковка_Ozon.xlsx"
    await cb.message.answer_document(
        BufferedInputFile(data, filename=fname),
        caption=(
            f"📋 Шаблон с твоими артикулами Ozon: <b>{st.total}</b> шт.\n"
            f"Уже заполнено: {st.with_hint}, осталось: {st.without_hint}.\n\n"
            "Заполни колонки <b>упаковка</b> и <b>примечание</b> и пришли файл обратно "
            "через «📤 Загрузить xlsx»."
        ),
    )


@router.callback_query(F.data == "phints:upload")
async def cb_upload_start(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    if not cb.message:
        return
    await state.set_state(ProductHints.awaiting_file)
    text = (
        "📤 <b>Жду xlsx-файл</b>\n\n"
        "Колонки в файле:\n"
        "• <b>артикул</b> — offer_id товара на Ozon\n"
        "• <b>упаковка</b> — как упаковывать (плёнка, мешок, …)\n"
        "• <b>примечание</b> — что важно знать (хрупкий, маркировка, …)\n\n"
        "Названия колонок гибкие: «артикул» / «offer_id», «упаковка», «примечание» / «комментарий»."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✖ Отмена", callback_data="phints:cancel")],
    ])
    await safe_edit_or_answer(cb.message, text, reply_markup=kb)


@router.callback_query(F.data == "phints:cancel")
async def cb_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer("Отменено")
    await state.clear()
    if cb.message:
        with db_session() as s:
            has_catalog = get_catalog_stats(s).total > 0
        await safe_edit_or_answer(
            cb.message, _build_intro_text(), reply_markup=_intro_kb(has_catalog),
        )


# ── Приём файла ──────────────────────────────────────────────────────────


@router.message(ProductHints.awaiting_file, F.document)
async def on_file(msg: Message, state: FSMContext) -> None:
    doc = msg.document
    fname = doc.file_name or "hints.xlsx"
    if not fname.lower().endswith((".xls", ".xlsx")):
        await msg.answer("Принимаю только .xlsx / .xls. Попробуй ещё раз или отмени.")
        return

    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    stored_path = STORAGE_DIR / f"hints_{ts}_{fname}"
    file = await msg.bot.get_file(doc.file_id)
    await msg.bot.download_file(file.file_path, destination=stored_path)

    try:
        rows = parse_hints_xlsx(stored_path)
    except ValueError as e:
        await msg.answer(f"❌ {e}\n\nПопробуй ещё раз или отмени.")
        return
    except Exception as e:
        logger.exception("Не смог распарсить xlsx: %s", e)
        await msg.answer(f"❌ Ошибка чтения xlsx: <code>{e}</code>")
        await state.clear()
        return

    if not rows:
        await msg.answer("📄 В файле нет строк с артикулами. Попробуй другой файл.")
        return

    with db_session() as s:
        report = resolve_rows(s, rows)

    # Сериализуем matched в state — небольшой объём, ок.
    matched_payload = [
        {
            "pid": r.ozon_product_id,
            "offer_id": r.offer_id,
            "raw": r.raw_article,
            "pack": r.packaging,
            "notes": r.notes,
        }
        for r in report.matched
    ]
    await state.update_data(matched=matched_payload)
    await state.set_state(ProductHints.confirm_strategy)

    lines = [
        "📋 <b>Результат разбора файла</b>\n",
        f"📂 Файл: <code>{fname}</code>",
        f"✅ Распознано в каталоге: <b>{len(report.matched)}</b>",
    ]
    if report.unmatched_articles:
        sample = ", ".join(f"<code>{a}</code>" for a in report.unmatched_articles[:10])
        more = f" … ещё {len(report.unmatched_articles) - 10}" if len(report.unmatched_articles) > 10 else ""
        lines.append(f"⚠ Не нашлось в каталоге: <b>{len(report.unmatched_articles)}</b>")
        lines.append(f"   {sample}{more}")
        lines.append("   <i>Эти артикулы будут пропущены. Проверь написание или обнови каталог.</i>")

    if not report.matched:
        lines.append("\n❌ Применять нечего. Отмени и проверь файл.")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✖ Отмена", callback_data="phints:cancel")],
        ])
    else:
        lines.append("\n<b>Как применить?</b>")
        lines.append("• 🔄 <b>Upsert</b> — обновим только эти артикулы, остальное в БД оставим как есть.")
        lines.append("• 🚮 <b>Заменить всё</b> — сотрём все hints и зальём только из файла.")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Upsert (рекомендую)", callback_data="phints:apply:upsert")],
            [InlineKeyboardButton(text="🚮 Заменить всё", callback_data="phints:apply:replace")],
            [InlineKeyboardButton(text="✖ Отмена", callback_data="phints:cancel")],
        ])

    await msg.answer("\n".join(lines), reply_markup=kb)


# ── Применение стратегии ─────────────────────────────────────────────────


def _matched_from_state(payload: list) -> List[ResolvedRow]:
    return [
        ResolvedRow(
            ozon_product_id=item["pid"],
            offer_id=item.get("offer_id") or "",
            raw_article=item.get("raw") or "",
            packaging=item.get("pack"),
            notes=item.get("notes"),
        )
        for item in payload
    ]


@router.callback_query(ProductHints.confirm_strategy, F.data == "phints:apply:upsert")
async def cb_apply_upsert(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer("Применяю…")
    data = await state.get_data()
    matched = _matched_from_state(data.get("matched") or [])
    with db_session() as s:
        n = apply_upsert(s, matched)
        st = get_catalog_stats(s)
    await state.clear()
    text = (
        "✅ <b>Готово — Upsert</b>\n\n"
        f"Обновлено/добавлено: <b>{n}</b> товаров\n\n"
        f"📦 В каталоге: {st.total}\n"
        f"✅ С данными: {st.with_hint}\n"
        f"⚠ Без данных: {st.without_hint}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Залить ещё файл", callback_data="phints:upload")],
        [InlineKeyboardButton(text="◀ В настройки", callback_data="menu:settings")],
    ])
    if cb.message:
        await safe_edit_or_answer(cb.message, text, reply_markup=kb)


@router.callback_query(ProductHints.confirm_strategy, F.data == "phints:apply:replace")
async def cb_apply_replace(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer("Заменяю…")
    data = await state.get_data()
    matched = _matched_from_state(data.get("matched") or [])
    with db_session() as s:
        deleted, inserted = apply_replace(s, matched)
        st = get_catalog_stats(s)
    await state.clear()
    text = (
        "🚮 <b>Готово — Заменено</b>\n\n"
        f"Удалено старых записей: <b>{deleted}</b>\n"
        f"Залито из файла: <b>{inserted}</b>\n\n"
        f"📦 В каталоге: {st.total}\n"
        f"✅ С данными: {st.with_hint}\n"
        f"⚠ Без данных: {st.without_hint}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Залить ещё файл", callback_data="phints:upload")],
        [InlineKeyboardButton(text="◀ В настройки", callback_data="menu:settings")],
    ])
    if cb.message:
        await safe_edit_or_answer(cb.message, text, reply_markup=kb)
