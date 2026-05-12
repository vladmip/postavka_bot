from aiogram import Router, F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext

from src.bot.helpers import safe_edit_or_answer
from src.bot.keyboards import kb_intake_mode, kb_pick_sku
from src.bot.states import SkuAdd, SkuKitAdd
from src.db.session import db_session
from src.services.catalog_service import (
    list_skus, find_sku_by_article, find_sku_by_barcode, upsert_sku,
    add_kit_component, get_kit_components, get_sku,
)

router = Router()


@router.message(Command("sku_list"))
async def cmd_sku_list(msg: Message) -> None:
    with db_session() as session:
        skus = list_skus(session, limit=80)
        if not skus:
            await msg.answer("Каталог пуст. Используй /sku_add или запусти scripts/seed_catalog.py")
            return
        lines = [f"📦 SKU в каталоге: {len(skus)}\n"]
        for s in skus:
            kit_mark = " 🎁" if get_kit_components(session, s.id) else ""
            lines.append(f"<code>{s.article}</code> — {s.name[:40]}{kit_mark}")
        await msg.answer("\n".join(lines))


@router.message(Command("sku_add"))
async def cmd_sku_add(msg: Message, state: FSMContext) -> None:
    await state.set_state(SkuAdd.barcode)
    await msg.answer("Шаг 1/4. Отправь ШК (баркод):")


@router.message(SkuAdd.barcode)
async def sku_add_barcode(msg: Message, state: FSMContext) -> None:
    bc = (msg.text or "").strip()
    if not bc:
        return
    with db_session() as session:
        if find_sku_by_barcode(session, bc):
            await msg.answer(f"SKU с баркодом {bc} уже есть. Отмена.")
            await state.clear()
            return
    await state.update_data(barcode=bc)
    await state.set_state(SkuAdd.article)
    await msg.answer("Шаг 2/4. Артикул (например MILK-CHOCOLATE):")


@router.message(SkuAdd.article)
async def sku_add_article(msg: Message, state: FSMContext) -> None:
    art = (msg.text or "").strip()
    if not art:
        return
    await state.update_data(article=art)
    await state.set_state(SkuAdd.name)
    await msg.answer("Шаг 3/4. Название товара:")


@router.message(SkuAdd.name)
async def sku_add_name(msg: Message, state: FSMContext) -> None:
    name = (msg.text or "").strip()
    if not name:
        return
    await state.update_data(name=name)
    await state.set_state(SkuAdd.intake_mode)
    await msg.answer("Шаг 4/4. Как ФФ принимает товар?", reply_markup=kb_intake_mode())


@router.callback_query(SkuAdd.intake_mode, F.data.startswith("im:"))
async def sku_add_intake(cb: CallbackQuery, state: FSMContext) -> None:
    mode = cb.data.split(":", 1)[1]
    data = await state.get_data()
    with db_session() as session:
        sku, _created = upsert_sku(
            session,
            barcode=data["barcode"],
            article=data["article"],
            name=data["name"],
            intake_mode=mode,
        )
    await state.clear()
    await cb.answer("Добавлено")
    if cb.message:
        await safe_edit_or_answer(cb.message, f"✅ SKU создан: <code>{data['article']}</code> ({data['barcode']})")


@router.message(Command("sku_kit_add"))
async def cmd_sku_kit_add(msg: Message, command: CommandObject, state: FSMContext) -> None:
    if not command.args:
        await msg.answer("Использование: /sku_kit_add <article>\nНапример: /sku_kit_add 3CHOC")
        return
    article = command.args.strip()
    with db_session() as session:
        kit = find_sku_by_article(session, article)
        if not kit:
            await msg.answer(f"SKU {article!r} не найден.")
            return
        skus = list_skus(session, limit=80)
    await state.update_data(kit_sku_id=kit.id, kit_article=kit.article)
    await state.set_state(SkuKitAdd.pick_component)
    await msg.answer(
        f"Выбери компонент для набора <b>{kit.article}</b>:",
        reply_markup=kb_pick_sku(skus, prefix="kit_comp"),
    )


@router.callback_query(SkuKitAdd.pick_component, F.data.startswith("kit_comp:"))
async def kit_add_component_picked(cb: CallbackQuery, state: FSMContext) -> None:
    component_sku_id = int(cb.data.split(":", 1)[1])
    await state.update_data(component_sku_id=component_sku_id)
    await state.set_state(SkuKitAdd.qty)
    await cb.answer()
    if cb.message:
        await safe_edit_or_answer(cb.message, "Сколько штук компонента в одном наборе?")


@router.message(SkuKitAdd.qty)
async def kit_add_qty(msg: Message, state: FSMContext) -> None:
    try:
        qty = int((msg.text or "").strip())
    except ValueError:
        await msg.answer("Нужно число.")
        return
    data = await state.get_data()
    with db_session() as session:
        add_kit_component(
            session,
            kit_sku_id=data["kit_sku_id"],
            component_sku_id=data["component_sku_id"],
            qty=qty,
        )
        comp = get_sku(session, data["component_sku_id"])
        comp_article = comp.article if comp else "?"
    await state.clear()
    await msg.answer(
        f"✅ {data['kit_article']} ← {comp_article} × {qty}"
    )
