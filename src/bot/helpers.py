"""Утилиты для безопасной работы с Telegram API.

Также — accumulating progress log (одна «сарделька» в чате вместо 10 сообщений).
Идея: создаём одно сообщение, накапливаем строки и редактируем его (edit_text).
Если лимит 4096 близко — стартуем новое.
"""
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


# ── Accumulating progress log ─────────────────────────────────────────────


async def progress_start(msg: Message, state, header: str) -> None:
    """Создать одно status-сообщение или продолжить существующее.

    Если в state есть ob_progress_msg_id — добавляем header как новую строку
    к существующей сардельке (через progress_add). Иначе — новое сообщение.
    """
    data = await state.get_data()
    if data.get("ob_progress_msg_id"):
        # Уже есть сарделька — просто добавляем заголовок к ней
        await progress_add(msg, state, header)
        return
    m = await msg.answer(header)
    await state.update_data(ob_progress_msg_id=m.message_id, ob_progress_text=header)


async def progress_add(msg: Message, state, line: str) -> None:
    """Дописать строку в накопительный status-message. edit_text если влезает,
    иначе стартует новое сообщение."""
    data = await state.get_data()
    msg_id = data.get("ob_progress_msg_id")
    cur = data.get("ob_progress_text") or ""
    new = (cur + "\n" + line) if cur else line
    if msg_id and len(new) < 3800:
        try:
            await msg.bot.edit_message_text(
                new, chat_id=msg.chat.id, message_id=msg_id,
            )
            await state.update_data(ob_progress_text=new)
            return
        except Exception as e:
            logger.debug("progress edit_text failed: %s — start new", e)
    m = await msg.answer(line)
    await state.update_data(ob_progress_msg_id=m.message_id, ob_progress_text=line)


async def progress_reset(state) -> None:
    """Сбросить (например после завершения флоу) — чтобы следующий вызов начал новое."""
    await state.update_data(ob_progress_msg_id=None, ob_progress_text=None)
