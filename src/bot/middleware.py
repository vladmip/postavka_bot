"""Middleware для логирования всех апдейтов, обработки исключений + rate-limit."""
import html
import logging
import time
import traceback
from collections import defaultdict, deque
from typing import Any, Awaitable, Callable, Dict, Deque

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message, TelegramObject, Update

from src.db.session import db_session
from src.services.user_service import get_or_create_user

logger = logging.getLogger("bot.middleware")


def _extract_user_event(event: TelegramObject):
    """Из Update извлечь Message или CallbackQuery если они есть."""
    if isinstance(event, Update):
        return event.message or event.callback_query
    return event


_SECRET_HEX_RE = None  # ленивая компиляция

def _looks_like_secret(text: str) -> bool:
    """Эвристика: строка похожа на Ozon API key (hex/uuid, >=20 символов,
    без пробелов). Цель — не логировать токены из onboarding-сообщений.
    Ложноположительные (длинный артикул) — допустимы, лучше потерять контент
    в логе, чем утечь ключ."""
    if not text:
        return False
    s = text.strip()
    if " " in s or "\n" in s or len(s) < 20:
        return False
    # Hex-only / uuid с дефисами / base64-like
    global _SECRET_HEX_RE
    if _SECRET_HEX_RE is None:
        import re
        _SECRET_HEX_RE = re.compile(r"^[A-Za-z0-9_\-+/=]+$")
    return bool(_SECRET_HEX_RE.match(s))


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
            # Защита от утечки токенов в лог: если строка похожа на API key
            # (длинная hex/uuid), маскируем тело. Onboarding шаги шлют именно
            # такие строки. Лучше пропустить useful-content в логе чем
            # утечь Ozon API key в bot.log.
            if _looks_like_secret(text):
                text = f"<masked secret, len={len(text)}>"
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


class EnsureUserMiddleware(BaseMiddleware):
    """Гарантирует запись в users для каждого активного tg_id.
    Заменяет старый OnlyAllowedUser-фильтр (теперь бот публичный).
    Также не логирует text при вероятных onboarding-сообщениях с токенами."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        inner = _extract_user_event(event)
        tg_id = None
        if isinstance(inner, (Message, CallbackQuery)) and inner.from_user:
            tg_id = inner.from_user.id
        if tg_id:
            try:
                with db_session() as s:
                    get_or_create_user(s, tg_id)
            except Exception:
                logger.exception("ensure_user failed for tg_id=%s", tg_id)
        return await handler(event, data)


class RateLimitMiddleware(BaseMiddleware):
    """In-memory rate-limit per tg_id: max_per_min действий/минуту,
    max_per_hour действий/час. Превышение → ответ «Слишком часто» + drop.

    Для single-process бота этого хватает; persistent счётчики не нужны.
    Защита от спама / DoS через массовые callback'и.
    """

    def __init__(self, max_per_min: int = 30, max_per_hour: int = 200) -> None:
        super().__init__()
        self.max_per_min = max_per_min
        self.max_per_hour = max_per_hour
        # Двойная очередь timestamps на user_id: храним только за последний час.
        self._hits: Dict[int, Deque[float]] = defaultdict(deque)

    def _allow(self, tg_id: int) -> bool:
        now = time.time()
        q = self._hits[tg_id]
        # Чистим старше часа
        cutoff_h = now - 3600
        while q and q[0] < cutoff_h:
            q.popleft()
        # Считаем за последнюю минуту
        cutoff_m = now - 60
        in_min = sum(1 for t in q if t >= cutoff_m)
        if in_min >= self.max_per_min:
            return False
        if len(q) >= self.max_per_hour:
            return False
        q.append(now)
        return True

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        inner = _extract_user_event(event)
        tg_id = None
        if isinstance(inner, (Message, CallbackQuery)) and inner.from_user:
            tg_id = inner.from_user.id
        if tg_id and not self._allow(tg_id):
            try:
                if isinstance(inner, CallbackQuery):
                    await inner.answer("⚠ Слишком часто. Подожди минуту.", show_alert=True)
                elif isinstance(inner, Message):
                    await inner.answer("⚠ Слишком часто — подожди минуту, бот тебя притормозил.")
            except Exception:
                pass
            logger.warning("rate-limited tg_id=%s (in_min=%d)",
                           tg_id, sum(1 for t in self._hits[tg_id] if t >= time.time()-60))
            return None
        return await handler(event, data)


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
