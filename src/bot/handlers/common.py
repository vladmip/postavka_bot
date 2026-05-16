"""Главное меню + общие команды.

Принцип: вся навигация через инлайн-кнопки, сообщения редактируются (не плодим).
Команды /start, /help — единственные точки входа. Внутри — кнопки.
"""
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.fsm.context import FSMContext

from src.bot.helpers import safe_edit_or_answer

router = Router()


# ── Меню ──────────────────────────────────────────────────────────────────


# URL гайда «как пользоваться» (Telegraph).
ONBOARDING_URL = "https://telegra.ph/Postavkinbot-bot-pomoshchnik-dlya-FBOFBW-postavok-05-13"


def _main_menu_kb(tg_id: int | None = None) -> InlineKeyboardMarkup:
    from src.config import ADMIN_USER_IDS
    rows = [
        [InlineKeyboardButton(text="📋 Мои поставки", callback_data="menu:ships")],
        [InlineKeyboardButton(text="☀ Утренняя сводка", callback_data="menu:digest")],
        [InlineKeyboardButton(text="📥 Возвраты", callback_data="menu:returns")],
        [InlineKeyboardButton(text="⚙ Настройки", callback_data="menu:settings")],
        [InlineKeyboardButton(text="📖 Для новичка — как пользоваться",
                              url=ONBOARDING_URL)],
        [InlineKeyboardButton(text="📚 Справка", callback_data="menu:help")],
    ]
    if tg_id and tg_id in ADMIN_USER_IDS:
        rows.insert(-1, [InlineKeyboardButton(text="🛠 Админка",
                                              callback_data="menu:admin")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _settings_menu_kb() -> InlineKeyboardMarkup:
    # MVP: WB-фичи скрыты (sku_link только для Ozon, WB-коэффициенты скрыты).
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📑 Данные о товарах", callback_data="menu:product_hints")],
        [InlineKeyboardButton(text="⭐ Точки кроссдока", callback_data="menu:favorites")],
        [InlineKeyboardButton(text="🔄 Обновить каталог Ozon", callback_data="run:sku_link_ozon")],
        [InlineKeyboardButton(text="🔌 Проверить API-ключи", callback_data="diag:api_check")],
        [InlineKeyboardButton(text="🗑 Удалить мои данные (/forget_me)",
                              callback_data="forget_me:start")],
        [InlineKeyboardButton(text="◀ В главное меню", callback_data="menu:home")],
    ])


@router.callback_query(lambda c: c.data == "forget_me:start")
async def cb_forget_me_start(cb: CallbackQuery) -> None:
    """Кнопка из настроек — переходим на тот же flow что /forget_me."""
    if cb.message:
        await cb.answer()
        # Эмулируем msg-вызов: создаём фейковое сообщение через ответ юзера
        # не получается — просто вызовем cmd_forget_me с этим cb.message.
        # У cb.message нет from_user (это бот) — поэтому нужно cb.from_user.
        # Делаем inline-вариант здесь.
        from src.db.session import db_session
        from src.db.models import User, ShipmentRequest, OzonProduct, WbProduct, FavoriteCrossdockPoint
        tg_id = cb.from_user.id if cb.from_user else None
        if not tg_id:
            return
        with db_session() as s:
            u = s.get(User, tg_id)
            if not u:
                await safe_edit_or_answer(cb.message, "ℹ Тебя нет в базе — нечего удалять.",
                                          reply_markup=_back_to_menu_kb())
                return
            n_ships = s.query(ShipmentRequest).filter_by(user_id=tg_id).count()
            n_oz = s.query(OzonProduct).filter_by(user_id=tg_id).count()
            n_wb = s.query(WbProduct).filter_by(user_id=tg_id).count()
            n_fav = s.query(FavoriteCrossdockPoint).filter_by(user_id=tg_id).count()
        text = (
            "🗑 <b>Удалить твои данные?</b>\n\n"
            f"• Поставки: {n_ships}\n• Ozon-каталог: {n_oz}\n"
            f"• WB-каталог: {n_wb}\n• Точки кроссдока: {n_fav}\n"
            f"• API-ключи и подсказки\n\n<b>Это необратимо.</b>"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Да, удалить всё", callback_data="forget_me:confirm")],
            [InlineKeyboardButton(text="✖ Отмена", callback_data="menu:home")],
        ])
        await safe_edit_or_answer(cb.message, text, reply_markup=kb)


_MAIN_TEXT = (
    "👋 <b>Бот-помощник по поставкам Ozon FBO</b>\n\n"
    "<b>Как пользоваться:</b>\n"
    "1. Кидаешь .xlsx-выгрузку по кластеру → бот создаёт <b>поставку</b>\n"
    "2. Тапаешь по поставке → планируешь даты\n"
    "3. Бот ищет слоты и сам бронирует в ЛК Ozon\n\n"
    "👇 Выбери действие:"
)


_HELP_TEXT = (
    "📚 <b>Справка</b>\n\n"
    "🚀 <b>Основной flow:</b>\n"
    "1. Кидаешь .xlsx с Ozon → бот парсит и создаёт поставку.\n"
    "2. Открываешь поставку через «📋 Мои поставки».\n"
    "3. Планируешь даты и часы отгрузки.\n"
    "4. «Забронировать слоты» → бот через API бронирует в Ozon ЛК.\n"
    "5. Кнопка «📤 ТЗ xlsx» в карточке поставки — генерирует ТЗ Отгрузки для ФФ.\n\n"
    "⚙ <b>Настройки</b>:\n"
    "  📑 Данные о товарах — упаковка/примечание, попадает в ТЗ.\n"
    "  ⭐ Точки кроссдока — избранные drop-off хабы.\n"
    "  🔄 Обновить каталог Ozon — sync с твоим кабинетом.\n"
    "  🔌 Проверить API-ключи.\n"
    "  🗑 /forget_me — стереть все твои данные.\n\n"
    "📥 <b>Загрузка файлов:</b>\n"
    "Просто отправь боту xlsx/xls — бот сам разберётся.\n\n"
    "🧭 Навигация только через инлайн-кнопки.\n"
    "<i>WB-поддержка пока в разработке (write-API закрыт у Wildberries).</i>"
)


def _back_to_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:home")],
    ])


def _sku_link_kb() -> InlineKeyboardMarkup:
    # MVP: только Ozon. WB-каталог скрыт.
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔵 Обновить Ozon каталог", callback_data="run:sku_link_ozon")],
        [InlineKeyboardButton(text="◀ В настройки", callback_data="menu:settings")],
    ])


# ── /start (только команда — единственная точка входа без кнопок) ─────────


def _release_ozon_locks() -> None:
    """Сброс зависших wizard-локов Ozon (single-tenant — чистим все).

    Раньше стух 30-мин wizard-лок ловился сообщением «⏳ Ozon-мастер уже запущен»
    без способа разлочить — теперь /start снимает все.
    """
    try:
        from src.bot.handlers.ozon_book import _WIZARD_IN_FLIGHT, _DRAFTS_CREATING
        _WIZARD_IN_FLIGHT.clear()
        _DRAFTS_CREATING.clear()
    except Exception:
        pass


@router.message(Command("start"))
async def cmd_start(msg: Message, state: FSMContext) -> None:
    _release_ozon_locks()
    # Если юзер ещё без токенов — запустим onboarding wizard вместо меню.
    from src.bot.handlers.onboarding import maybe_start_onboarding
    if await maybe_start_onboarding(msg, state):
        return
    tg_id = msg.from_user.id if msg.from_user else None
    await msg.answer(_MAIN_TEXT, reply_markup=_main_menu_kb(tg_id))


# ── menu:home — главное меню (edit существующего сообщения) ──────────────


@router.callback_query(lambda c: c.data == "menu:home")
async def cb_menu_home(cb: CallbackQuery) -> None:
    await cb.answer()
    if cb.message:
        tg_id = cb.from_user.id if cb.from_user else None
        await safe_edit_or_answer(cb.message, _MAIN_TEXT, reply_markup=_main_menu_kb(tg_id))


# ── menu:help ─────────────────────────────────────────────────────────────


@router.callback_query(lambda c: c.data == "menu:help")
async def cb_menu_help(cb: CallbackQuery) -> None:
    await cb.answer()
    if cb.message:
        await safe_edit_or_answer(cb.message, _HELP_TEXT, reply_markup=_back_to_menu_kb())


@router.message(Command("help"))
async def cmd_help(msg: Message) -> None:
    await msg.answer(_HELP_TEXT, reply_markup=_back_to_menu_kb())


# ── menu:ships ────────────────────────────────────────────────────────────


@router.callback_query(lambda c: c.data == "menu:ships")
async def cb_menu_ships(cb: CallbackQuery) -> None:
    await cb.answer()
    if cb.message:
        from src.bot.handlers.shipment import _render_ship_list
        tg_id = cb.from_user.id if cb.from_user else 0
        await _render_ship_list(cb.message, tg_id=tg_id, edit=True)


# ── menu:returns ──────────────────────────────────────────────────────────


@router.callback_query(lambda c: c.data == "menu:digest")
async def cb_menu_digest(cb: CallbackQuery) -> None:
    """Кнопка «☀ Утренняя сводка» — то же что /digest."""
    await cb.answer("Собираю сводку…")
    if cb.message:
        await safe_edit_or_answer(cb.message, "☀ Собираю сводку…")
        from src.bot.handlers.digest import send_digest_to_user
        await send_digest_to_user(cb.message.bot, cb.message.chat.id)


@router.callback_query(lambda c: c.data == "menu:returns")
async def cb_menu_returns(cb: CallbackQuery) -> None:
    """Сразу Ozon-возвраты (WB пока скрыт). Раньше было подменю выбора
    маркетплейса — лишний клик при единственном активном MP."""
    from src.bot.handlers.returns import cb_ret_ozon
    await cb_ret_ozon(cb)


# ── menu:sku_link ─────────────────────────────────────────────────────────


@router.callback_query(lambda c: c.data == "menu:sku_link")
async def cb_menu_sku_link(cb: CallbackQuery) -> None:
    await cb.answer()
    if cb.message:
        text = (
            "🔗 <b>Привязка каталога к маркетплейсам</b>\n\n"
            "Бот возьмёт твой каталог SKU и подтянет nm_id (WB) / offer_id+sku (Ozon) "
            "по штрихкоду. Нужно: чтобы товары были загружены в ЛК маркетплейса."
        )
        await safe_edit_or_answer(cb.message, text, reply_markup=_sku_link_kb())


@router.callback_query(lambda c: c.data == "run:sku_link_wb")
async def cb_run_sku_link_wb(cb: CallbackQuery) -> None:
    await cb.answer("Запускаю…")
    if cb.message:
        from src.bot.handlers.integrations import cmd_sku_link_wb
        await cmd_sku_link_wb(cb.message, _tg_id=cb.from_user.id if cb.from_user else None)


@router.callback_query(lambda c: c.data == "run:sku_link_ozon")
async def cb_run_sku_link_ozon(cb: CallbackQuery) -> None:
    await cb.answer("Запускаю…")
    if cb.message and cb.from_user:
        from src.bot.handlers.integrations import cmd_sku_link_ozon
        # cb.message.from_user — это бот; передаём tg_id юзера явно
        await cmd_sku_link_ozon(cb.message, _tg_id=cb.from_user.id)




# ── menu:settings — настройки и сервисные операции ───────────────────────


@router.callback_query(lambda c: c.data == "menu:settings")
async def cb_menu_settings(cb: CallbackQuery) -> None:
    await cb.answer()
    if cb.message:
        text = (
            "⚙ <b>Настройки</b>\n\n"
            "📑 <b>Данные о товарах</b> — упаковка и примечание на каждый артикул, "
            "попадают в ТЗ Отгрузки.\n"
            "⭐ <b>Точки кроссдока</b> — избранные drop-off хабы.\n"
            "🔗 <b>Привязать каталог к МП</b> — освежить связь твоего каталога с "
            "карточками WB/Ozon (по баркоду).\n"
            "🔌 <b>Проверить API-ключи</b> — пинг WB/Ozon.\n"
            "📊 <b>WB коэффициенты приёмки</b> — справочно."
        )
        await safe_edit_or_answer(cb.message, text, reply_markup=_settings_menu_kb())


@router.callback_query(lambda c: c.data == "diag:api_check")
async def cb_diag_api_check(cb: CallbackQuery) -> None:
    await cb.answer("Проверяю…")
    if cb.message:
        from src.bot.handlers.integrations import cmd_api_check
        await cmd_api_check(cb.message, _tg_id=cb.from_user.id if cb.from_user else None)


@router.callback_query(lambda c: c.data == "diag:wb_coefs")
async def cb_diag_wb_coefs(cb: CallbackQuery) -> None:
    await cb.answer("Тяну коэффициенты WB…")
    if cb.message:
        from src.bot.handlers.integrations import cmd_wb_coefs
        await cmd_wb_coefs(cb.message, _tg_id=cb.from_user.id if cb.from_user else None)


# ── inline-callback «✖ Отмена» в wizard'ах ────────────────────────────────


@router.callback_query(lambda c: c.data == "cancel")
async def cb_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    _release_ozon_locks()
    await cb.answer("Отменено")
    if cb.message:
        # После отмены — сразу возвращаем в меню
        tg_id = cb.from_user.id if cb.from_user else None
        await safe_edit_or_answer(cb.message, _MAIN_TEXT, reply_markup=_main_menu_kb(tg_id))


# ── /forget_me — удалить все данные юзера (GDPR + удобство тестов) ────────

@router.message(Command("forget_me"))
async def cmd_forget_me(msg: Message) -> None:
    """Шаг 1: показывает что будет удалено + кнопку подтверждения."""
    tg_id = msg.from_user.id if msg.from_user else None
    if not tg_id:
        return
    from src.db.session import db_session
    from src.db.models import User, ShipmentRequest, OzonProduct, WbProduct, FavoriteCrossdockPoint
    with db_session() as s:
        u = s.get(User, tg_id)
        if not u:
            await msg.answer("ℹ Тебя нет в базе — нечего удалять.")
            return
        n_ships = s.query(ShipmentRequest).filter_by(user_id=tg_id).count()
        n_oz = s.query(OzonProduct).filter_by(user_id=tg_id).count()
        n_wb = s.query(WbProduct).filter_by(user_id=tg_id).count()
        n_fav = s.query(FavoriteCrossdockPoint).filter_by(user_id=tg_id).count()
    text = (
        "🗑 <b>Удалить твои данные?</b>\n\n"
        f"Будет стёрто:\n"
        f"• Поставки: {n_ships}\n"
        f"• Ozon-каталог: {n_oz}\n"
        f"• WB-каталог: {n_wb}\n"
        f"• Любимые точки кроссдока: {n_fav}\n"
        f"• API-ключи и подсказки к товарам\n\n"
        f"<b>Это необратимо.</b>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Да, удалить всё", callback_data="forget_me:confirm")],
        [InlineKeyboardButton(text="✖ Отмена", callback_data="menu:home")],
    ])
    await msg.answer(text, reply_markup=kb)


@router.callback_query(lambda c: c.data == "forget_me:confirm")
async def cb_forget_me_confirm(cb: CallbackQuery, state: FSMContext) -> None:
    tg_id = cb.from_user.id if cb.from_user else None
    if not tg_id:
        return
    await cb.answer("Удаляю…")
    from src.db.session import db_session
    from src.db.models import User
    with db_session() as s:
        u = s.get(User, tg_id)
        if u:
            # ON DELETE CASCADE прибьёт shipment_requests, ozon_products,
            # wb_products, favorite_crossdock_points через user_id FK.
            # ProductHint каскадно за OzonProduct.
            s.delete(u)
    await state.clear()
    _release_ozon_locks()
    if cb.message:
        await safe_edit_or_answer(
            cb.message,
            "✅ Все твои данные удалены. Нажми /start чтобы зарегистрироваться заново.",
        )


# ── /admin_stats — мини-админка (только для ALLOWED_USER_ID) ──────────────

@router.message(Command("admin_stats"))
async def cmd_admin_stats(msg: Message) -> None:
    from src.config import ADMIN_USER_IDS
    tg_id = msg.from_user.id if msg.from_user else None
    if not tg_id or tg_id not in ADMIN_USER_IDS:
        return  # молча игнорируем — команда не для всех
    from src.db.session import db_session
    from src.db.models import User, ShipmentRequest
    from datetime import datetime, timedelta
    with db_session() as s:
        total_users = s.query(User).count()
        onboarded = s.query(User).filter(User.onboarded_at.is_not(None)).count()
        recent_cutoff = datetime.utcnow() - timedelta(days=7)
        active_7d = s.query(ShipmentRequest).filter(
            ShipmentRequest.created_at >= recent_cutoff,
        ).count()
        ships_total = s.query(ShipmentRequest).count()
    text = (
        "👤 <b>Админ-статистика</b>\n\n"
        f"Всего юзеров: <b>{total_users}</b>\n"
        f"Onboarded (есть Ozon-ключи): <b>{onboarded}</b>\n"
        f"Поставок всего: <b>{ships_total}</b>\n"
        f"Поставок за 7д: <b>{active_7d}</b>\n"
    )
    await msg.answer(text)
