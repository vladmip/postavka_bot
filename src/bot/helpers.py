"""Утилиты для безопасной работы с Telegram API."""
import logging
from typing import Optional

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message, InlineKeyboardMarkup

logger = logging.getLogger("bot.helpers")


async def safe_edit_or_answer(
    msg: Message,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
    """edit_text → если не получилось (нельзя редактировать сообщение, отсутствует и т.п.) → answer."""
    try:
        await msg.edit_text(text, reply_markup=reply_markup)
        return
    except TelegramBadRequest as e:
        logger.warning("edit_text failed: %s — fallback to answer()", e)
    except Exception as e:
        logger.warning("edit_text unexpected: %s — fallback to answer()", e)

    try:
        await msg.answer(text, reply_markup=reply_markup)
    except Exception as e:
        logger.exception("answer() also failed: %s", e)


TG_MSG_LIMIT = 3900  # реальный лимит 4096, оставляем запас на HTML-теги


async def send_long(msg: Message, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None) -> None:
    """Отправить длинный текст несколькими сообщениями. Разбивает по строкам."""
    if len(text) <= TG_MSG_LIMIT:
        await msg.answer(text, reply_markup=reply_markup)
        return

    parts = []
    cur = ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > TG_MSG_LIMIT:
            if cur:
                parts.append(cur)
                cur = ""
            # одна строка длиннее лимита — режем грубо
            while len(line) > TG_MSG_LIMIT:
                parts.append(line[:TG_MSG_LIMIT])
                line = line[TG_MSG_LIMIT:]
        cur = (cur + "\n" + line) if cur else line
    if cur:
        parts.append(cur)

    last_idx = len(parts) - 1
    for i, p in enumerate(parts):
        rm = reply_markup if i == last_idx else None
        try:
            await msg.answer(p, reply_markup=rm)
        except Exception as e:
            logger.exception("send_long part %d failed: %s", i, e)
