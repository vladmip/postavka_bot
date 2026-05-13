import asyncio
import logging
import logging.handlers
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Filter
from aiogram.types import Message, CallbackQuery, TelegramObject, BotCommand

from src.config import TELEGRAM_BOT_TOKEN, ALLOWED_USER_ID
from src.bot.handlers import common, catalog, upload, integrations, shipment, ozon_book
from src.bot.middleware import LogAndCatchMiddleware


def _setup_logging() -> None:
    """Логирование одновременно в консоль и в logs/bot.log (rotated по 5 МБ × 5 файлов)."""
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Очищаем дефолтные хендлеры (на случай повторной инициализации)
    for h in list(root.handlers):
        root.removeHandler(h)
    # Консоль
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    root.addHandler(ch)
    # Файл с ротацией
    fh = logging.handlers.RotatingFileHandler(
        log_dir / "bot.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)
    # Шумные либы — приглушаем до WARNING
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)


_setup_logging()


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

    # Регистрируем команды в панели Telegram (левая «menu» кнопка)
    await bot.set_my_commands([
        BotCommand(command="start", description="🏠 Главное меню"),
        BotCommand(command="ship", description="📋 Мои заявки"),
        BotCommand(command="help", description="📚 Справка"),
        BotCommand(command="cancel", description="✖ Отменить текущий мастер"),
    ])

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
