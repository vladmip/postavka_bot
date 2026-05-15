"""Onboarding wizard для новых пользователей.

Flow при /start, если у юзера нет Ozon-токенов:
1. Шаг 1/3: «Введи Client ID Ozon» → юзер пишет → бот удаляет сообщение → сохраняет.
2. Шаг 2/3: «Теперь API Key Ozon» → то же.
3. Шаг 3/3: «Загрузи товарное наличие» — объясняет зачем, кнопка-запуск рефреша каталога.
4. Финал: инструкция + переход в меню.

Сообщения юзера с токенами удаляются СРАЗУ — чтобы ключи не висели в чате.
"""
from __future__ import annotations

import logging

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.fsm.context import FSMContext

from src.bot.helpers import safe_edit_or_answer
from src.bot.states import Onboarding
from src.db.session import db_session
from src.services.catalog_service import refresh_ozon_catalog
from src.services.user_service import (
    get_or_create_user, get_ozon_client_for, is_onboarded,
    save_ozon_creds, validate_ozon_creds,
)

logger = logging.getLogger("handlers.onboarding")
router = Router()

ONBOARDING_GUIDE_URL = "https://telegra.ph/Postavkinbot-bot-pomoshchnik-dlya-FBOFBW-postavok-05-13"


def _kb_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✖ Отмена", callback_data="onb:cancel")],
    ])


async def maybe_start_onboarding(msg: Message, state: FSMContext) -> bool:
    """Если у юзера нет токенов — запустить wizard. Возвращает True если запущен.
    Вызывается из /start вместо стандартного показа меню."""
    tg_id = msg.from_user.id if msg.from_user else None
    if not tg_id:
        return False
    with db_session() as s:
        u = get_or_create_user(s, tg_id)
        ok = is_onboarded(u)
    if ok:
        return False

    await state.set_state(Onboarding.ozon_client_id)
    await msg.answer(
        "👋 <b>Привет! Это бот-помощник по поставкам Ozon FBO.</b>\n\n"
        "Чтобы начать, мне нужны API-доступы Ozon.\n\n"
        "1️⃣ Зайди на <b>seller.ozon.ru → Настройки → Seller API</b>\n"
        "2️⃣ Создай новый API-ключ (роль <b>«Admin»</b>).\n"
        "3️⃣ Скопируй <b>Client ID</b> (число вверху страницы — твой ID кабинета).\n\n"
        "<b>Шаг 1/2 — пришли Client ID</b> отдельным сообщением.\n"
        "Я удалю его из чата сразу после получения.",
        reply_markup=_kb_cancel(),
    )
    return True


@router.message(Onboarding.ozon_client_id)
async def on_client_id(msg: Message, state: FSMContext) -> None:
    raw = (msg.text or "").strip()
    # Удаляем сообщение юзера с токеном немедленно.
    try:
        await msg.delete()
    except Exception:
        pass
    if not raw or not raw.isdigit():
        await msg.answer(
            "⚠ Client ID — это число. Попробуй ещё раз.",
            reply_markup=_kb_cancel(),
        )
        return
    await state.update_data(client_id=raw)
    await state.set_state(Onboarding.ozon_api_key)
    await msg.answer(
        "✅ Client ID получен.\n\n"
        "<b>Шаг 2/2 — пришли API Key</b> (длинная строка вида <code>5e0d…</code>).\n"
        "Тоже удалю сразу. После проверки бот сразу подтянет твой каталог.",
        reply_markup=_kb_cancel(),
    )


@router.message(Onboarding.ozon_api_key)
async def on_api_key(msg: Message, state: FSMContext) -> None:
    raw = (msg.text or "").strip()
    try:
        await msg.delete()
    except Exception:
        pass
    if len(raw) < 20:
        await msg.answer(
            "⚠ API Key слишком короткий. Скопируй полностью.",
            reply_markup=_kb_cancel(),
        )
        return
    data = await state.get_data()
    client_id = data.get("client_id", "")
    tg_id = msg.from_user.id if msg.from_user else None
    if not tg_id or not client_id:
        await msg.answer("⚠ Состояние потеряно. Нажми /start заново.")
        await state.clear()
        return

    # Validate: тестовый запрос product_list(limit=1). Не сохраняем мусорные ключи.
    status_msg = await msg.answer("🔐 Проверяю ключи в Ozon API…")
    ok, err = await validate_ozon_creds(client_id, raw)
    if not ok:
        # Остаёмся в state.ozon_api_key — юзер пробует другой ключ.
        await status_msg.edit_text(
            f"❌ <b>Ключи не подходят.</b>\n\n{err}\n\n"
            f"Пришли API Key ещё раз (Client ID уже сохранён).",
            reply_markup=_kb_cancel(),
        )
        return

    # ОК → сохраняем (api_key шифруется Fernet'ом внутри save_ozon_creds).
    with db_session() as s:
        save_ozon_creds(s, tg_id, client_id, raw)
    await state.clear()
    await status_msg.edit_text("✅ Ключи валидны. Тяну каталог из Ozon…")

    # Автозагрузка каталога. Если что-то упало — не критично, юзер потом
    # вручную через настройки.
    try:
        with db_session() as s:
            oz = get_ozon_client_for(s, tg_id)
            if oz is None:
                raise RuntimeError("get_ozon_client_for вернул None после save")
            result = await refresh_ozon_catalog(s, oz, tg_id)
        await status_msg.edit_text(
            f"✅ <b>Готово!</b>\n\n"
            f"Каталог Ozon синхронизирован:\n"
            f"  • новых: {result.added}\n"
            f"  • обновлено: {result.updated}\n"
            f"  • всего товаров: {result.total}\n"
        )
    except Exception as e:
        logger.exception("auto-refresh catalog failed")
        await status_msg.edit_text(
            f"✅ Ключи сохранены, но <b>каталог не подгрузился</b>:\n"
            f"<code>{type(e).__name__}: {str(e)[:200]}</code>\n\n"
            f"Можно повторить через ⚙ Настройки → 🔄 Обновить каталог Ozon."
        )

    await _send_done_message(msg)


async def _send_done_message(msg: Message) -> None:
    text = (
        "🎉 <b>Готово!</b> Подключение настроено.\n\n"
        "📖 <b>Краткая инструкция:</b>\n"
        "1. Кидай боту xlsx-выгрузку по кластеру (получаешь из ЛК Ozon).\n"
        "2. Открой заявку через «📋 Мои заявки» → «🛠 Спланировать даты».\n"
        "3. Тапни «🚀 Создать поставку Ozon» — бот забронирует слоты через API.\n"
        "4. Скачай ТЗ Отгрузки и пришли ФФ.\n\n"
        f"Полный гайд: {ONBOARDING_GUIDE_URL}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 В главное меню", callback_data="menu:home")],
    ])
    await msg.answer(text, reply_markup=kb)


@router.callback_query(F.data == "onb:cancel")
async def cb_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer("Отменено")
    await state.clear()
    if cb.message:
        await safe_edit_or_answer(
            cb.message,
            "✖ Подключение отменено. Нажми /start чтобы попробовать снова.",
        )
