"""Главное меню + общие команды.

Принцип: вся навигация через инлайн-кнопки, сообщения редактируются (не плодим).
Команды /start, /help, /cancel — только точки входа. Внутри — кнопки.
"""
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.fsm.context import FSMContext

from src.bot.helpers import safe_edit_or_answer

router = Router()


# ── Меню ──────────────────────────────────────────────────────────────────


def _main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Мои заявки", callback_data="menu:ships")],
        [InlineKeyboardButton(text="🔗 Привязать каталог к МП", callback_data="menu:sku_link")],
        [InlineKeyboardButton(text="🛠 Диагностика", callback_data="menu:diag")],
        [InlineKeyboardButton(text="📚 Справка", callback_data="menu:help")],
    ])


_MAIN_TEXT = (
    "👋 <b>Бот-помощник по поставкам</b>\nИП Баковец × ЛЕБЕР\n\n"
    "<b>Как пользоваться:</b>\n"
    "1. Кидаешь .xlsx-выгрузку по кластеру → бот создаёт <b>заявку</b>\n"
    "2. Тапаешь по заявке → планируешь даты\n"
    "3. Бот ищет слоты и подсказывает что делать дальше\n\n"
    "👇 Выбери действие:"
)


_HELP_TEXT = (
    "📚 <b>Справка</b>\n\n"
    "🚀 <b>Основной flow:</b>\n"
    "1. Кидаешь .xlsx с маркетплейса (WB или Ozon)\n"
    "2. Бот парсит → создаёт заявку\n"
    "3. Открываешь заявку через «📋 Мои заявки»\n"
    "4. WB: «Подобрать склад» → даты → завершаешь в WB ЛК (API не даёт)\n"
    "5. Ozon: «Создать поставку» → бот через API создаёт черновик в ЛК\n\n"
    "🧭 <b>Навигация:</b>\n"
    "Всё через кнопки. Возврат — кнопкой «◀ Назад» или «🏠 Меню».\n"
    "Команды: только /start (открыть меню), /cancel (отменить мастер).\n\n"
    "📥 <b>Загрузка файлов:</b>\n"
    "Просто отправь боту xlsx/xls — он сам определит:\n"
    "  • выгрузку по кластеру → создаст/дополнит заявку\n"
    "  • опись коробов → привяжет к поставке\n"
    "  • prihod / Остатки → сверит расхождения"
)


def _back_to_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:home")],
    ])


def _diag_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔌 Проверить API-ключи", callback_data="diag:api_check")],
        [InlineKeyboardButton(text="🔥 Прогреть кэши складов", callback_data="diag:api_warmup")],
        [InlineKeyboardButton(text="📊 WB коэффициенты приёмки", callback_data="diag:wb_coefs")],
        [InlineKeyboardButton(text="🏭 Ozon кластеры FBO", callback_data="diag:ozon_warehouses")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="menu:home")],
    ])


def _sku_link_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟣 Привязать к Wildberries", callback_data="run:sku_link_wb")],
        [InlineKeyboardButton(text="🔵 Привязать к Ozon", callback_data="run:sku_link_ozon")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="menu:home")],
    ])


# ── /start (только команда — единственная точка входа без кнопок) ─────────


@router.message(Command("start"))
async def cmd_start(msg: Message) -> None:
    await msg.answer(_MAIN_TEXT, reply_markup=_main_menu_kb())


# ── menu:home — главное меню (edit существующего сообщения) ──────────────


@router.callback_query(lambda c: c.data == "menu:home")
async def cb_menu_home(cb: CallbackQuery) -> None:
    await cb.answer()
    if cb.message:
        await safe_edit_or_answer(cb.message, _MAIN_TEXT, reply_markup=_main_menu_kb())


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
        await _render_ship_list(cb.message, edit=True)


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
        # Привязка длинная и пишет прогресс — отдельные сообщения, это OK
        await cmd_sku_link_wb(cb.message)


@router.callback_query(lambda c: c.data == "run:sku_link_ozon")
async def cb_run_sku_link_ozon(cb: CallbackQuery) -> None:
    await cb.answer("Запускаю…")
    if cb.message:
        from src.bot.handlers.integrations import cmd_sku_link_ozon
        await cmd_sku_link_ozon(cb.message)


# ── menu:diag — подменю диагностики ──────────────────────────────────────


@router.callback_query(lambda c: c.data == "menu:diag")
async def cb_menu_diag(cb: CallbackQuery) -> None:
    await cb.answer()
    if cb.message:
        text = (
            "🛠 <b>Диагностика и сервис</b>\n\n"
            "Здесь редко нужные операции:\n"
            "• проверка ключей API\n"
            "• прогрев кэшей складов (ускоряет первый /ship_hunt)\n"
            "• сводка коэффициентов WB / кластеров Ozon"
        )
        await safe_edit_or_answer(cb.message, text, reply_markup=_diag_menu_kb())


@router.callback_query(lambda c: c.data == "diag:api_check")
async def cb_diag_api_check(cb: CallbackQuery) -> None:
    await cb.answer("Проверяю…")
    if cb.message:
        from src.bot.handlers.integrations import cmd_api_check
        await cmd_api_check(cb.message)


@router.callback_query(lambda c: c.data == "diag:api_warmup")
async def cb_diag_warmup(cb: CallbackQuery) -> None:
    await cb.answer("Прогреваю…")
    if cb.message:
        from src.bot.handlers.integrations import cmd_api_warmup
        await cmd_api_warmup(cb.message)


@router.callback_query(lambda c: c.data == "diag:wb_coefs")
async def cb_diag_wb_coefs(cb: CallbackQuery) -> None:
    await cb.answer("Тяну коэффициенты WB…")
    if cb.message:
        from src.bot.handlers.integrations import cmd_wb_coefs
        await cmd_wb_coefs(cb.message)


@router.callback_query(lambda c: c.data == "diag:ozon_warehouses")
async def cb_diag_ozon_wh(cb: CallbackQuery) -> None:
    await cb.answer("Тяну Ozon-кластеры…")
    if cb.message:
        from src.bot.handlers.integrations import cmd_ozon_warehouses
        await cmd_ozon_warehouses(cb.message)


# ── /cancel — отмена FSM-мастера ─────────────────────────────────────────


@router.callback_query(lambda c: c.data == "cancel")
async def cb_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    cur = await state.get_state()
    await state.clear()
    await cb.answer("Отменено")
    if cb.message:
        # После отмены — сразу возвращаем в меню
        await safe_edit_or_answer(cb.message, _MAIN_TEXT, reply_markup=_main_menu_kb())


@router.message(Command("cancel"))
async def cmd_cancel(msg: Message, state: FSMContext) -> None:
    cur = await state.get_state()
    await state.clear()
    await msg.answer(f"✖ Отменено (был state: {cur or 'none'}). /start чтобы открыть меню.")
