from io import BytesIO

from aiogram import Router, F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, CallbackQuery, BufferedInputFile

from src.db.session import db_session
from src.generators import generate_tz_priemka, generate_tz_otgruzka
from src.generators.tz_priemka import PriemkaRow
from src.generators.tz_otgruzka import OtgruzkaRow
from src.services.supply_service import get_supply

router = Router()


def _build_priemka_rows(supply) -> list:
    rows = []
    for it in supply.items:
        # для приёмки: kit-строки (без expanded_from_kit_id) и обычные одиночки.
        # Если у этого supply_item.sku есть в этой поставке компоненты с expanded_from_kit_id == sku_id —
        # это kit, пишем его, компоненты пропускаем.
        is_kit_parent = any(o.expanded_from_kit_id == it.sku_id for o in supply.items)
        is_component = it.expanded_from_kit_id is not None
        if is_component:
            continue
        if not it.sku:
            continue
        rows.append(PriemkaRow(
            barcode=it.sku.barcode,
            name=it.sku.name,
            qty=it.qty_planned,
            supplier_article=it.sku.article,
        ))
    return rows


def _build_otgruzka_rows(supply) -> list:
    rows = []
    for it in supply.items:
        # для отгрузки: компоненты (expanded_from_kit_id != None) и обычные одиночки.
        # kit-строки пропускаем.
        is_kit_parent = any(o.expanded_from_kit_id == it.sku_id for o in supply.items)
        if is_kit_parent and it.expanded_from_kit_id is None:
            continue
        if not it.sku:
            continue
        rows.append(OtgruzkaRow(
            barcode=it.sku.barcode,
            name=it.sku.name,
            qty=it.qty_planned,
            warehouse=supply.warehouse,
            marketplace=supply.marketplace,
            supplier_article=it.sku.article,
        ))
    return rows


@router.message(Command("supply_export_intake"))
async def cmd_export_intake(msg: Message, command: CommandObject) -> None:
    try:
        supply_id = int((command.args or "").strip())
    except ValueError:
        await msg.answer("Использование: /supply_export_intake <supply_id>")
        return
    await _send_intake(msg, supply_id)


@router.callback_query(F.data.startswith("exp_in:"))
async def cb_export_intake(cb: CallbackQuery) -> None:
    supply_id = int(cb.data.split(":", 1)[1])
    await cb.answer("Генерирую ТЗ Приёмка…")
    if cb.message:
        await _send_intake(cb.message, supply_id)


async def _send_intake(target: Message, supply_id: int) -> None:
    with db_session() as session:
        supply = get_supply(session, supply_id)
        if not supply:
            await target.answer(f"Поставка #{supply_id} не найдена.")
            return
        rows = _build_priemka_rows(supply)
    if not rows:
        await target.answer("В поставке нет позиций.")
        return
    data = generate_tz_priemka(rows)
    fname = f"TZ_Priemka_supply_{supply_id}.xlsx"
    await target.answer_document(
        document=BufferedInputFile(data, filename=fname),
        caption=f"📥 ТЗ Приёмка поставки #{supply_id} ({len(rows)} строк). Перешли ФФ.",
    )


@router.message(Command("supply_export_shipment"))
async def cmd_export_shipment(msg: Message, command: CommandObject) -> None:
    try:
        supply_id = int((command.args or "").strip())
    except ValueError:
        await msg.answer("Использование: /supply_export_shipment <supply_id>")
        return
    await _send_shipment(msg, supply_id)


@router.callback_query(F.data.startswith("exp_out:"))
async def cb_export_shipment(cb: CallbackQuery) -> None:
    supply_id = int(cb.data.split(":", 1)[1])
    await cb.answer("Генерирую ТЗ Отгрузка…")
    if cb.message:
        await _send_shipment(cb.message, supply_id)


async def _send_shipment(target: Message, supply_id: int) -> None:
    with db_session() as session:
        supply = get_supply(session, supply_id)
        if not supply:
            await target.answer(f"Поставка #{supply_id} не найдена.")
            return
        rows = _build_otgruzka_rows(supply)
    if not rows:
        await target.answer("В поставке нет позиций.")
        return
    data = generate_tz_otgruzka(rows)
    fname = f"TZ_Otgruzka_supply_{supply_id}.xlsx"
    await target.answer_document(
        document=BufferedInputFile(data, filename=fname),
        caption=f"📤 ТЗ Отгрузка поставки #{supply_id} ({len(rows)} строк). Перешли ФФ.",
    )
