"""/digest — утренняя сводка владельцу.

Команда дёргается:
  • вручную (юзер пишет /digest или жмёт кнопку «☀ Сводка сейчас» в меню)
  • автоматически из bot.main scheduler в 09:00 МСК

Функция send_digest_to_user(bot, chat_id) reusable — вызывается из обоих мест.
"""
from __future__ import annotations

import logging
from io import BytesIO

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram import F

from src.bot.helpers import safe_edit_or_answer, send_long
from src.db.session import db_session
from src.services.digest_service import build_digest_text, collect_digest
from src.services.user_service import get_ozon_client_for

router = Router()
logger = logging.getLogger("bot.digest")


def _back_kb() -> InlineKeyboardMarkup:
    # Кнопки «🔄 Обновить» нет специально: сводка считается дорого (4-5 API-вызовов
    # к Ozon, ~5 секунд). Если юзеру нужна свежая — пишет /digest.
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:home")],
    ])


async def send_digest_to_user(bot: Bot, chat_id: int) -> None:
    """Собрать сводку и отправить в чат. Используется и scheduler'ом, и /digest.

    PDF этикетки шлётся отдельным document-сообщением до текста сводки —
    document не editable, поэтому он стоит «выше», а текст с кнопками — «ниже».
    """
    # chat_id у user'а в private-чате == tg_id. Берём именно chat_id чтобы
    # scheduler мог дёргать send_digest_to_user(bot, tg_id) → шлёт в личку.
    with db_session() as s:
        oz = get_ozon_client_for(s, chat_id)
    if oz is None:
        await bot.send_message(
            chat_id,
            "⚠ Ozon-ключи не подключены. Нажми /start чтобы пройти онбординг.",
        )
        return
    try:
        data = await collect_digest(oz)
    except Exception as e:
        logger.exception("collect_digest failed")
        await bot.send_message(chat_id, f"❌ Не собралась сводка: <code>{str(e)[:300]}</code>")
        return

    text = build_digest_text(data)

    # PDF этикетка возвратов относится только к returns/giveouts (на ПВЗ).
    # Removals — это вывозы со стока FBO, у них своя логистика, эта этикетка
    # к ним не относится. Раньше PDF летел в чат даже когда нет actionable
    # returns (есть только removals) и сбивал юзера.
    r = data.returns
    has_returns_actionable = bool(r.total or r.giveouts_available or r.giveouts_at_pvz)
    if data.returns.pdf_bytes and has_returns_actionable:
        try:
            file = BufferedInputFile(data.returns.pdf_bytes, filename="ozon_returns.pdf")
            await bot.send_document(
                chat_id, file,
                caption="📄 Этикетка возвратов — приложить на ПВЗ.",
            )
        except Exception as e:
            logger.warning("send_document(pdf) failed: %s", e)

    await bot.send_message(chat_id, text, reply_markup=_back_kb())


@router.message(Command("digest"))
async def cmd_digest(msg: Message, bot: Bot) -> None:
    """Ручной триггер сводки. Шлёт прогресс-плейсхолдер, потом затирает."""
    placeholder = await msg.answer("☀ Собираю сводку…")
    try:
        await send_digest_to_user(bot, msg.chat.id)
    finally:
        try:
            await placeholder.delete()
        except Exception:
            pass


# callback digest:refresh снят: кнопка «🔄 Обновить» убрана из клавиатуры,
# юзер вызывает /digest заново если нужна свежая сводка.
