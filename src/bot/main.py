import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Filter
from aiogram.types import Message, CallbackQuery, TelegramObject

from src.config import TELEGRAM_BOT_TOKEN, ALLOWED_USER_ID
from src.bot.handlers import common, catalog, upload, integrations, shipment, ozon_book
from src.bot.middleware import LogAndCatchMiddleware

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


class OnlyAllowedUser(Filter):
    """Игнорирует всех кроме ALLOWED_USER_ID. Если ALLOWED_USER_ID=0 — пропускает всех."""

    async def __call__(self, event: TelegramObject) -> bool:
        if ALLOWED_USER_ID == 0:
            return True
        user_id = None
        if isinstance(event, Message):
            user_id = event.from_user.id if event.from_user else None
        elif isinstance(event, CallbackQuery):
            user_id = event.from_user.id if event.from_user else None
        return user_id == ALLOWED_USER_ID


async def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN не задан в .env. "
            "Получи токен у @BotFather и положи в .env: TELEGRAM_BOT_TOKEN=...\n"
            "Также добавь ALLOWED_USER_ID=<твой Telegram user id> (узнать у @userinfobot)."
        )

    bot = Bot(token=TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()

    log_mw = LogAndCatchMiddleware()
    dp.update.outer_middleware(log_mw)

    user_filter = OnlyAllowedUser()
    for r in (common.router, catalog.router,
              shipment.router, ozon_book.router, upload.router, integrations.router):
        r.message.filter(user_filter)
        r.callback_query.filter(user_filter)
        dp.include_router(r)

    me = await bot.get_me()
    logging.info("Bot started: @%s (id=%s)", me.username, me.id)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
