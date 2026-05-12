from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.fsm.context import FSMContext

from src.bot.helpers import safe_edit_or_answer

router = Router()


def _main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Мои заявки", callback_data="menu:ships")],
        [InlineKeyboardButton(text="🔗 Привязать каталог к маркетплейсам", callback_data="menu:sku_link")],
        [InlineKeyboardButton(text="🔌 Проверить API", callback_data="menu:api_check")],
        [InlineKeyboardButton(text="📚 Справка", callback_data="menu:help")],
    ])


@router.message(Command("start"))
async def cmd_start(msg: Message) -> None:
    await msg.answer(
        "👋 <b>Бот-помощник по поставкам</b>\nИП Баковец × ЛЕБЕР\n\n"
        "<b>Как пользоваться:</b>\n"
        "1. Кидаешь .xlsx-выгрузку по кластеру → бот создаёт <b>заявку</b>\n"
        "2. Тапаешь по заявке → планируешь даты\n"
        "3. Бот ищет слоты и подсказывает что делать дальше\n\n"
        "👇 Выбери действие:",
        reply_markup=_main_menu_kb(),
    )


@router.callback_query(lambda c: c.data == "menu:help")
async def cb_menu_help(cb: CallbackQuery) -> None:
    await cb.answer()
    if cb.message:
        await cmd_help(cb.message)


@router.callback_query(lambda c: c.data == "menu:ships")
async def cb_menu_ships(cb: CallbackQuery) -> None:
    await cb.answer()
    if cb.message:
        from src.bot.handlers.shipment import _render_ship_list
        await _render_ship_list(cb.message, edit=True)


@router.callback_query(lambda c: c.data == "menu:sku_link")
async def cb_menu_sku_link(cb: CallbackQuery) -> None:
    await cb.answer()
    if cb.message:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🟣 Привязать к Wildberries", callback_data="run:sku_link_wb")],
            [InlineKeyboardButton(text="🔵 Привязать к Ozon", callback_data="run:sku_link_ozon")],
            [InlineKeyboardButton(text="◀ Назад", callback_data="menu:home")],
        ])
        await cb.message.answer(
            "🔗 <b>Привязка каталога к маркетплейсам</b>\n\n"
            "Бот возьмёт твой каталог SKU и подтянет nm_id (WB) / offer_id+sku (Ozon) "
            "по штрихкоду. Нужно: чтобы товары были загружены в ЛК маркетплейса.",
            reply_markup=kb,
        )


@router.callback_query(lambda c: c.data == "menu:api_check")
async def cb_menu_api(cb: CallbackQuery) -> None:
    await cb.answer("Проверяю…")
    if cb.message:
        from src.bot.handlers.integrations import cmd_api_check
        await cmd_api_check(cb.message)


@router.callback_query(lambda c: c.data == "menu:home")
async def cb_menu_home(cb: CallbackQuery) -> None:
    await cb.answer()
    if cb.message:
        try:
            await cb.message.edit_text("🏠 Главное меню:", reply_markup=_main_menu_kb())
        except Exception:
            await cb.message.answer("🏠 Главное меню:", reply_markup=_main_menu_kb())


@router.callback_query(lambda c: c.data == "run:sku_link_wb")
async def cb_run_sku_link_wb(cb: CallbackQuery) -> None:
    await cb.answer("Привязываю…")
    if cb.message:
        from src.bot.handlers.integrations import cmd_sku_link_wb
        await cmd_sku_link_wb(cb.message)


@router.callback_query(lambda c: c.data == "run:sku_link_ozon")
async def cb_run_sku_link_ozon(cb: CallbackQuery) -> None:
    await cb.answer("Привязываю…")
    if cb.message:
        from src.bot.handlers.integrations import cmd_sku_link_ozon
        await cmd_sku_link_ozon(cb.message)


@router.message(Command("help"))
async def cmd_help(msg: Message) -> None:
    text = (
        "📚 <b>Справка</b>\n\n"

        "🚀 <b>Основной flow:</b>\n"
        "1. Кидаешь .xlsx с маркетплейса (WB или Ozon)\n"
        "2. Бот парсит → создаёт заявку\n"
        "3. Тапаешь по заявке → выбираешь даты → бот ищет слоты\n"
        "4. WB: тапаешь склад → видишь даты → завершаешь в WB ЛК (API не даёт)\n"
        "5. Ozon: тапаешь склад → бот через API создаёт поставку в ЛК\n\n"

        "📋 <b>Главные команды:</b>\n"
        "/start — главное меню\n"
        "/ship — мои заявки\n\n"

        "🔌 <b>Диагностика (по необходимости):</b>\n"
        "/api_check — проверка ключей\n"
        "/wb_coefs — текущие коэф приёмки + логистики WB\n"
        "/api_warmup — прогрев кэшей складов\n\n"

        "<i>Всё остальное — через кнопки в карточке заявки.</i>"
    )
    await msg.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:home")],
    ]))


@router.callback_query(lambda c: c.data == "cancel")
async def cb_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    cur = await state.get_state()
    await state.clear()
    await cb.answer("Отменено")
    if cb.message:
        await safe_edit_or_answer(cb.message, f"✖ Отменено (был state: {cur or 'none'})")


@router.message(Command("cancel"))
async def cmd_cancel(msg: Message, state: FSMContext) -> None:
    cur = await state.get_state()
    await state.clear()
    await msg.answer(f"✖ Отменено (был state: {cur or 'none'})")
