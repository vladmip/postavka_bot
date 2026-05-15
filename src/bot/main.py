import asyncio
import logging
import logging.handlers
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Filter
from aiogram.types import Message, CallbackQuery, TelegramObject, BotCommand

from src.config import TELEGRAM_BOT_TOKEN, ALLOWED_USER_ID
from src.bot.handlers import common, catalog, upload, integrations, shipment, ozon_book, returns, favorites, product_hints, onboarding, digest
from src.bot.handlers.digest import send_digest_to_user
from src.bot.middleware import LogAndCatchMiddleware, EnsureUserMiddleware, RateLimitMiddleware

# Утренняя сводка — каждый день в это время по МСК (UTC+3).
DIGEST_HOUR_MSK = 9
DIGEST_MINUTE_MSK = 0


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
    """ОТКЛЮЧЁН в multi-tenant: бот теперь публичный.
    Оставлен для совместимости с handlers, что фильтр всегда возвращает True.
    Гарантия записи в users делается через EnsureUserMiddleware."""

    async def __call__(self, event: TelegramObject) -> bool:
        return True


async def _digest_scheduler(bot: Bot) -> None:
    """Цикл: спит до ближайшего 09:00 МСК → для каждого onboarded user
    выполняет _morning_routine (refresh каталога + статусов поставок + digest).
    Между юзерами sleep 2с чтобы не упереться в Telegram rate-limit
    (30 msg/sec global). MSK = UTC+3.

    asyncio.sleep вместо APScheduler — single-process bot, зависимостей не плодим.
    """
    msk_offset = timedelta(hours=3)
    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            now_msk = now_utc + msk_offset
            target_msk = now_msk.replace(
                hour=DIGEST_HOUR_MSK, minute=DIGEST_MINUTE_MSK, second=0, microsecond=0,
            )
            if target_msk <= now_msk:
                target_msk += timedelta(days=1)
            sleep_sec = (target_msk - now_msk).total_seconds()
            logging.info(
                "MORNING scheduler: sleep %.0f сек до %s МСК",
                sleep_sec, target_msk.strftime("%d.%m %H:%M"),
            )
            await asyncio.sleep(sleep_sec)
            recipients = _get_digest_recipients()
            logging.info("MORNING: рассылка по %d юзерам", len(recipients))
            for tg_id in recipients:
                try:
                    await _morning_routine_for_user(bot, tg_id)
                except Exception:
                    logging.exception("MORNING: failed for tg_id=%s", tg_id)
                await asyncio.sleep(2)  # щадим Telegram rate-limit
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("MORNING scheduler: ошибка, повтор через час")
            await asyncio.sleep(3600)


async def _morning_routine_for_user(bot: Bot, tg_id: int) -> None:
    """Утренняя рутина для одного юзера:
       1. Обновить Ozon-каталог (live → ozon_products юзера)
       2. Обновить статусы активных Ozon-поставок (refresh_supply_status)
       3. Отправить digest (внутри сводка ещё раз live дёргает returns_list,
          stocks_fbo, postings_fbo за 28д — это и есть «возвраты + продажи»).

    Любая ошибка на шаге 1-2 не блокирует digest. Логируем и идём дальше —
    юзеру не должно быть пусто из-за временного 429.
    """
    from src.db.session import db_session
    from src.db.models import ShipmentRequest
    from src.services.user_service import get_ozon_client_for
    from src.services.catalog_service import refresh_ozon_catalog
    from src.services.ozon_supply_status_service import refresh_supply_status

    # 1. Каталог
    try:
        with db_session() as s:
            oz = get_ozon_client_for(s, tg_id)
            if oz is None:
                logging.info("MORNING tg_id=%s: нет Ozon-кред, пропускаю каталог", tg_id)
            else:
                result = await refresh_ozon_catalog(s, oz, tg_id)
                logging.info(
                    "MORNING tg_id=%s catalog: +%d / ✎%d / -%d (всего %d)",
                    tg_id, result.added, result.updated, result.deleted, result.total,
                )
    except Exception:
        logging.exception("MORNING tg_id=%s: catalog refresh failed", tg_id)

    # 2. Статусы поставок (только активные: ещё не closed/cancelled, имеют ozon-направления)
    try:
        with db_session() as s:
            oz = get_ozon_client_for(s, tg_id)
            if oz is not None:
                # Ищем заявки юзера в активных state'ах с забронированными ozon-items.
                from src.config import ALLOWED_USER_ID
                q = s.query(ShipmentRequest).filter(
                    ShipmentRequest.state.notin_(("closed", "cancelled")),
                )
                # Multi-tenant: для ALLOWED_USER_ID берём + legacy NULL.
                if tg_id == ALLOWED_USER_ID:
                    q = q.filter((ShipmentRequest.user_id == tg_id) | (ShipmentRequest.user_id.is_(None)))
                else:
                    q = q.filter(ShipmentRequest.user_id == tg_id)
                active_reqs = q.all()
                touched_total = 0
                for req in active_reqs:
                    has_ozon = any(it.marketplace == "ozon" and it.booked_supply_id for it in req.items)
                    if not has_ozon:
                        continue
                    try:
                        n = await refresh_supply_status(s, oz, req.id, force=True)
                        touched_total += n
                    except Exception:
                        logging.exception(
                            "MORNING tg_id=%s rid=%d: refresh_supply_status failed",
                            tg_id, req.id,
                        )
                if touched_total:
                    logging.info(
                        "MORNING tg_id=%s supply_status обновлён для %d items",
                        tg_id, touched_total,
                    )
    except Exception:
        logging.exception("MORNING tg_id=%s: supply_status refresh failed", tg_id)

    # 3. Digest (live тянет возвраты + 28-дневные продажи)
    await send_digest_to_user(bot, tg_id)


def _get_digest_recipients() -> list[int]:
    """Все юзеры с готовым onboarding (есть Ozon credentials).
    Дополнительно — ALLOWED_USER_ID (если задан в .env), даже если в БД
    нет onboarded_at — для legacy-владельца кабинета."""
    from src.db.session import db_session
    from src.db.models import User
    out: set = set()
    if ALLOWED_USER_ID:
        out.add(ALLOWED_USER_ID)
    try:
        with db_session() as s:
            users = s.query(User).filter(User.onboarded_at.is_not(None)).all()
            for u in users:
                if u.ozon_client_id and u.ozon_api_key:
                    out.add(u.tg_id)
    except Exception:
        logging.exception("DIGEST: не получилось взять список юзеров")
    return sorted(out)


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
    dp.update.outer_middleware(EnsureUserMiddleware())
    dp.update.outer_middleware(RateLimitMiddleware(max_per_min=30, max_per_hour=200))

    user_filter = OnlyAllowedUser()
    # onboarding.router идёт ДО common.router — иначе callback'и onboarding'а
    # перехватываются main menu router'ом первыми.
    for r in (onboarding.router, common.router, catalog.router, product_hints.router,
              shipment.router, ozon_book.router, upload.router, integrations.router,
              returns.router, favorites.router, digest.router):
        r.message.filter(user_filter)
        r.callback_query.filter(user_filter)
        dp.include_router(r)

    me = await bot.get_me()
    logging.info("Bot started: @%s (id=%s)", me.username, me.id)

    # Регистрируем команды в панели Telegram (левая «menu» кнопка).
    # Минимум: /start — главное меню (всё остальное оттуда), /help — справка.
    await bot.set_my_commands([
        BotCommand(command="start", description="🏠 Главное меню"),
        BotCommand(command="digest", description="☀ Утренняя сводка"),
        BotCommand(command="help", description="📚 Справка"),
    ])

    await bot.delete_webhook(drop_pending_updates=True)

    scheduler_task = asyncio.create_task(_digest_scheduler(bot))
    try:
        await dp.start_polling(bot)
    finally:
        scheduler_task.cancel()
        try:
            await scheduler_task
        except asyncio.CancelledError:
            pass


def run() -> None:
    """Entry point. Ловит KeyboardInterrupt / SystemExit для graceful shutdown
    (asyncio.run сам отменит таски при выходе)."""
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Shutdown requested — stopping bot.")
    except Exception:
        logging.exception("Bot crashed at top level")
        raise


if __name__ == "__main__":
    run()
