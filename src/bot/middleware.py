"""Middleware для логирования всех апдейтов в консоль и обработки исключений с алертом в чат."""
import html
import logging
import traceback
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message, TelegramObject, Update

logger = logging.getLogger("bot.middleware")


def _extract_user_event(event: TelegramObject):
    """Из Update извлечь Message или CallbackQuery если они есть."""
    if isinstance(event, Update):
        return event.message or event.callback_query
    return event


class LogAndCatchMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        inner = _extract_user_event(event)
        if isinstance(inner, Message):
            user = inner.from_user.id if inner.from_user else "?"
            text = inner.text or (inner.document.file_name if inner.document else "<no text>")
            logger.info("MSG from %s: %s", user, text[:120])
        elif isinstance(inner, CallbackQuery):
            user = inner.from_user.id if inner.from_user else "?"
            logger.info("CB  from %s: %s", user, inner.data)

        try:
            return await handler(event, data)
        except TelegramBadRequest as e:
            logger.exception("TelegramBadRequest: %s", e)
            err_text = (
                f"⚠ Telegram отверг запрос:\n<code>{html.escape(str(e))}</code>"
            )
            await _alert(inner, err_text)
            return None
        except Exception as e:
            logger.exception("Handler error: %s", e)
            err_text = (
                f"⚠ Ошибка в обработчике:\n<code>{html.escape(type(e).__name__)}: {html.escape(str(e))}</code>"
            )
            await _alert(inner, err_text)
            return None


async def _alert(event, err_text: str) -> None:
    try:
        if isinstance(event, Message):
            await event.answer(err_text)
        elif isinstance(event, CallbackQuery):
            await event.answer(f"Ошибка — см. чат", show_alert=True)
            if event.message:
                await event.message.answer(err_text)
    except Exception:
        logger.exception("alert failed")
