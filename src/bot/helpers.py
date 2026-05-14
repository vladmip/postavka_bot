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


# ── Форматирование выбранных дат ─────────────────────────────────────────


def format_picked_dates(
    picks,
    fallback_from=None,
    fallback_to=None,
) -> str:
    """Вернуть «20.05, 23.05» для реально выбранных дат.

    Юзер выбирает конкретные даты в picker'е, а target_date_from/to — это просто
    min..max диапазон для Ozon-API. Раньше показывали «2026-05-20 — 2026-05-23»
    что вводило в заблуждение если юзер на самом деле выбрал [20, 23] (без 21-22).

    `picks` — список iso-строк типа `["2026-05-20", "2026-05-23"]` (из
    `req.target_dates_json`). Если picks пустой/None — fallback на диапазон
    `from..to` (для совместимости со старыми заявками без target_dates_json).

    Возвращает:
      • [20] → "20.05"
      • [20, 23] → "20.05, 23.05"
      • [20, 21, 22, 23] (подряд) → "20.05–23.05"
      • picks пустой, from=20, to=23 → "20.05–23.05"
      • picks пустой, from=20, to=None → "20.05"
    """
    from datetime import date, datetime, timedelta

    def _fmt(d) -> str:
        return f"{d.day:02d}.{d.month:02d}"

    def _to_date(x):
        if isinstance(x, datetime):
            return x.date()
        if isinstance(x, date):
            return x
        return date.fromisoformat(str(x))

    if picks:
        dates = sorted({_to_date(p) for p in picks})
        # Проверяем подрядность — если все дни идут без разрывов, выводим как диапазон
        if len(dates) >= 2:
            is_contiguous = all(
                (dates[i + 1] - dates[i]).days == 1
                for i in range(len(dates) - 1)
            )
            if is_contiguous:
                return f"{_fmt(dates[0])}–{_fmt(dates[-1])}"
        return ", ".join(_fmt(d) for d in dates)

    if fallback_from is None:
        return "—"
    d_from = _to_date(fallback_from)
    if fallback_to is None:
        return _fmt(d_from)
    d_to = _to_date(fallback_to)
    if d_to == d_from:
        return _fmt(d_from)
    return f"{_fmt(d_from)}–{_fmt(d_to)}"


def format_picked_hours(hours) -> str:
    """Превратить список часов 0..23 в человеко-читаемую подпись.

    Примеры:
      • None / пустой → "🎲 любое время"
      • [10] → "10–11"
      • [9, 10, 11] (подряд) → "09–12"
      • [10, 14, 15] → "10–11, 14–16"
    """
    if not hours:
        return "🎲 любое время"
    hh = sorted(set(int(h) for h in hours))
    # Группируем подряд идущие часы в окна
    windows: list = []
    start = hh[0]
    prev = hh[0]
    for h in hh[1:]:
        if h == prev + 1:
            prev = h
            continue
        windows.append((start, prev))
        start = prev = h
    windows.append((start, prev))
    return ", ".join(f"{s:02d}–{(e + 1) % 24:02d}" for s, e in windows)
