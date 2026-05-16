"""Админ-панель. Доступ только tg_id из ADMIN_USER_IDS.

Команды/кнопки:
  /admin            — открыть инлайн-меню
  📊 Статистика     — всего юзеров, onboarded, поставок за период
  👥 Юзеры          — список tg_id + onboarded_at + что есть из ключей
  📜 Лог            — последние 60 строк bot.log
  🔬 Ozon diag      — переиспользует /ozon_diag
  🔌 API check      — переиспользует /api_check
  ↺ Git rev         — текущий HEAD SHA + сообщение
  🧹 Чистка черновиков — переиспользует /clear_drafts

Принцип: каждая кнопка → лёгкий callback, перерисовываем то же сообщение
через edit_text (никаких новых сообщений).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from src.bot.helpers import safe_edit_or_answer, send_long
from src.config import ADMIN_USER_IDS
from src.db.session import db_session

router = Router()
logger = logging.getLogger("bot.admin")


def is_admin(tg_id: int | None) -> bool:
    return bool(tg_id and tg_id in ADMIN_USER_IDS)


def _admin_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="adm:stats")],
        [InlineKeyboardButton(text="👥 Юзеры", callback_data="adm:users")],
        [InlineKeyboardButton(text="📜 Свежий лог (60 строк)", callback_data="adm:log")],
        [InlineKeyboardButton(text="🔌 API check", callback_data="adm:api_check")],
        [InlineKeyboardButton(text="🔬 Ozon diag (draft/create)", callback_data="adm:ozon_diag")],
        [InlineKeyboardButton(text="↺ Версия (git HEAD)", callback_data="adm:gitrev")],
        [InlineKeyboardButton(text="🧹 Чистка моих черновиков", callback_data="adm:clear_drafts")],
        [InlineKeyboardButton(text="◀ В главное меню", callback_data="menu:home")],
    ])


_HEADER = "🛠 <b>Админ-панель</b>"


@router.message(Command("admin"))
async def cmd_admin(msg: Message) -> None:
    tg_id = msg.from_user.id if msg.from_user else None
    if not is_admin(tg_id):
        return  # молча игнорируем
    await msg.answer(_HEADER, reply_markup=_admin_menu_kb())


@router.callback_query(F.data == "menu:admin")
async def cb_menu_admin(cb: CallbackQuery) -> None:
    tg_id = cb.from_user.id if cb.from_user else None
    if not is_admin(tg_id):
        await cb.answer("⛔ Только для админов", show_alert=True)
        return
    await cb.answer()
    if cb.message:
        await safe_edit_or_answer(cb.message, _HEADER, reply_markup=_admin_menu_kb())


def _back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀ В админку", callback_data="menu:admin")],
    ])


@router.callback_query(F.data == "adm:stats")
async def cb_adm_stats(cb: CallbackQuery) -> None:
    if not is_admin(cb.from_user.id if cb.from_user else None):
        await cb.answer("⛔", show_alert=True)
        return
    await cb.answer("Считаю…")
    from src.db.models import User, ShipmentRequest
    with db_session() as s:
        total_users = s.query(User).count()
        onboarded = s.query(User).filter(User.onboarded_at.is_not(None)).count()
        recent_cutoff = datetime.utcnow() - timedelta(days=7)
        active_7d = s.query(ShipmentRequest).filter(
            ShipmentRequest.created_at >= recent_cutoff,
        ).count()
        ships_total = s.query(ShipmentRequest).count()
    text = (
        f"{_HEADER}\n\n"
        "📊 <b>Статистика</b>\n"
        f"Всего юзеров: <b>{total_users}</b>\n"
        f"Onboarded: <b>{onboarded}</b>\n"
        f"Поставок всего: <b>{ships_total}</b>\n"
        f"Поставок за 7 дней: <b>{active_7d}</b>"
    )
    if cb.message:
        await safe_edit_or_answer(cb.message, text, reply_markup=_back_kb())


@router.callback_query(F.data == "adm:users")
async def cb_adm_users(cb: CallbackQuery) -> None:
    if not is_admin(cb.from_user.id if cb.from_user else None):
        await cb.answer("⛔", show_alert=True)
        return
    await cb.answer()
    from src.db.models import User
    lines = [f"{_HEADER}\n", "👥 <b>Юзеры</b>"]
    with db_session() as s:
        users = s.query(User).order_by(User.created_at.asc()).all()
        for u in users:
            oz = "🔵" if u.ozon_api_key else "⚪"
            wb = "🟣" if u.wb_api_key else "⚪"
            on = u.onboarded_at.strftime("%d.%m") if u.onboarded_at else "—"
            lines.append(
                f"  <code>{u.tg_id}</code> {oz}{wb} · onboarded: {on}"
            )
    lines.append("\n<i>🔵=Ozon ключ · 🟣=WB ключ</i>")
    if cb.message:
        await safe_edit_or_answer(cb.message, "\n".join(lines), reply_markup=_back_kb())


@router.callback_query(F.data == "adm:log")
async def cb_adm_log(cb: CallbackQuery) -> None:
    if not is_admin(cb.from_user.id if cb.from_user else None):
        await cb.answer("⛔", show_alert=True)
        return
    await cb.answer()
    log_path = Path("logs/bot.log")
    if not log_path.exists():
        text = "logs/bot.log не найден."
    else:
        # Берём хвост файла. 60 строк × ~200 байт ≈ 12 КБ — безопасно.
        try:
            with log_path.open("rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 30_000))
                tail = f.read().decode("utf-8", errors="replace")
            lines = tail.splitlines()[-60:]
            text = "📜 <b>Лог (последние 60 строк)</b>\n\n<pre>" + (
                "\n".join(lines).replace("<", "&lt;").replace(">", "&gt;")
            ) + "</pre>"
        except Exception as e:
            text = f"Не прочитал лог: <code>{type(e).__name__}: {e}</code>"
    if cb.message:
        await send_long(cb.message, text, reply_markup=_back_kb())


@router.callback_query(F.data == "adm:gitrev")
async def cb_adm_gitrev(cb: CallbackQuery) -> None:
    if not is_admin(cb.from_user.id if cb.from_user else None):
        await cb.answer("⛔", show_alert=True)
        return
    await cb.answer()
    import subprocess
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True
        ).strip()
        subject = subprocess.check_output(
            ["git", "log", "-1", "--pretty=%s"], text=True
        ).strip()
        when = subprocess.check_output(
            ["git", "log", "-1", "--pretty=%ad", "--date=short"], text=True
        ).strip()
        text = (
            f"{_HEADER}\n\n"
            f"↺ <b>Версия</b>\n"
            f"  SHA: <code>{sha}</code>\n"
            f"  Дата: {when}\n"
            f"  Сообщение: <i>{subject}</i>"
        )
    except Exception as e:
        text = f"git rev failed: <code>{type(e).__name__}: {e}</code>"
    if cb.message:
        await safe_edit_or_answer(cb.message, text, reply_markup=_back_kb())


@router.callback_query(F.data == "adm:api_check")
async def cb_adm_api_check(cb: CallbackQuery) -> None:
    if not is_admin(cb.from_user.id if cb.from_user else None):
        await cb.answer("⛔", show_alert=True)
        return
    await cb.answer("Запускаю /api_check…")
    # Делегируем готовому handler'у — он умеет работать с msg.
    # cb.message.from_user — это бот, поэтому tg_id передаём явно.
    from src.bot.handlers.integrations import cmd_api_check
    if cb.message:
        await cmd_api_check(cb.message, _tg_id=cb.from_user.id if cb.from_user else None)


@router.callback_query(F.data == "adm:ozon_diag")
async def cb_adm_ozon_diag(cb: CallbackQuery) -> None:
    if not is_admin(cb.from_user.id if cb.from_user else None):
        await cb.answer("⛔", show_alert=True)
        return
    await cb.answer("Запускаю /ozon_diag…")
    from src.bot.handlers.integrations import cmd_ozon_diag
    if cb.message:
        await cmd_ozon_diag(cb.message, _tg_id=cb.from_user.id if cb.from_user else None)


@router.callback_query(F.data == "adm:clear_drafts")
async def cb_adm_clear_drafts(cb: CallbackQuery) -> None:
    if not is_admin(cb.from_user.id if cb.from_user else None):
        await cb.answer("⛔", show_alert=True)
        return
    # Реально-чищающий callback уже есть в shipment.py: clear_drafts:yes.
    # Тут — подтверждение. cb.message.from_user — это бот, нам нужен админ.
    await cb.answer()
    from src.bot.handlers.shipment import cmd_clear_drafts
    if cb.message:
        await cmd_clear_drafts(cb.message, _tg_id=cb.from_user.id if cb.from_user else None)
