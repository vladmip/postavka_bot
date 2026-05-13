"""Обработка пересланных пользователем xlsx/xls от ФФ.

Workflow:
1. Скачать файл во `data/storage/`
2. Классифицировать (router.classify_file)
3. Распарсить
4. Если parsed — спросить «к какой поставке привязать?»
5. После привязки — обновить supply_items (для опись_*) или показать расхождения (для prihod)
"""
import json
from datetime import datetime
from pathlib import Path

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext

from src.bot.helpers import safe_edit_or_answer
from src.bot.keyboards import kb_pick_supply
from src.bot.states import UploadBind
from src.config import STORAGE_DIR
from src.db.session import db_session
from src.db.models import InboxFile
from src.parsers import (
    classify_file, FileKind,
    parse_opis_wb, parse_opis_ozon, parse_prihod, parse_ostatki,
)
from src.services.supply_service import (
    list_supplies, attach_picked_qty, transition,
)
from src.services.reconciler import reconcile_prihod

router = Router()


_SCREENSHOTS_DIR = Path(__file__).parent.parent.parent.parent / "data" / "screenshots"
_SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)


@router.message(F.photo)
async def handle_photo(msg: Message) -> None:
    """Сохраняем скрины в data/screenshots/ — чтобы Claude мог их Read'ом читать."""
    photo = msg.photo[-1]  # самая большая версия
    ts = datetime.utcnow().strftime("%Y-%m-%d_%H-%M-%S")
    fname = f"{ts}_{photo.file_unique_id}.jpg"
    target = _SCREENSHOTS_DIR / fname
    file = await msg.bot.get_file(photo.file_id)
    await msg.bot.download_file(file.file_path, destination=target)
    caption_extra = f"\n💬 Подпись: <i>{msg.caption}</i>" if msg.caption else ""
    await msg.answer(
        f"📷 Скрин сохранён:\n<code>{target.as_posix()}</code>{caption_extra}\n\n"
        f"<i>Скажи мне в чате — я открою и посмотрю что на нём.</i>"
    )


@router.message(F.document)
async def handle_document(msg: Message, state: FSMContext) -> None:
    doc = msg.document
    fname = doc.file_name or "upload.bin"

    if not (fname.lower().endswith(".xls") or fname.lower().endswith(".xlsx")):
        await msg.answer("Принимаю только .xls / .xlsx.")
        return

    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    stored_path = STORAGE_DIR / f"{ts}_{fname}"

    file = await msg.bot.get_file(doc.file_id)
    await msg.bot.download_file(file.file_path, destination=stored_path)

    # Если имя файла похоже на ship-выгрузку → отдаём shipment handler
    from src.bot.handlers.shipment import looks_like_ship_file, handle_ship_document
    if looks_like_ship_file(fname):
        await handle_ship_document(msg, state, stored_path, fname)
        return

    kind = classify_file(stored_path)

    if kind == FileKind.UNKNOWN:
        await msg.answer(f"Не распознал тип файла: {fname}. Сохранил в storage/.")
        _save_inbox(stored_path, fname, kind.value, None, {})
        return

    try:
        parsed_summary, parsed_payload = _parse(stored_path, kind)
    except Exception as e:
        await msg.answer(f"⚠ Парсер упал на {fname}: {e}")
        _save_inbox(stored_path, fname, kind.value, None, {"error": str(e)})
        return

    inbox_id = _save_inbox(stored_path, fname, kind.value, None, parsed_payload)

    with db_session() as session:
        candidates = list_supplies(session, limit=20)

    if not candidates:
        await msg.answer(
            f"✅ Распознал: <b>{kind.value}</b>\n{parsed_summary}\n\n"
            f"Поставок в БД нет — привязать не к чему. Сначала /supply_new."
        )
        return

    await state.update_data(inbox_id=inbox_id, kind=kind.value, stored_path=str(stored_path))
    await state.set_state(UploadBind.pick_supply)
    await msg.answer(
        f"✅ Распознал: <b>{kind.value}</b>\n{parsed_summary}\n\n"
        f"К какой поставке привязать?",
        reply_markup=kb_pick_supply(candidates),
    )


@router.callback_query(UploadBind.pick_supply, F.data.startswith("bind:"))
async def cb_bind(cb: CallbackQuery, state: FSMContext) -> None:
    supply_id = int(cb.data.split(":", 1)[1])
    data = await state.get_data()
    kind = data["kind"]
    stored_path = Path(data["stored_path"])
    inbox_id = data["inbox_id"]

    await state.clear()

    with db_session() as session:
        inbox = session.get(InboxFile, inbox_id)
        if inbox:
            inbox.supply_id = supply_id

        if kind == FileKind.OPIS_WB.value:
            items = parse_opis_wb(stored_path)
            opis_tuples = [(i.barcode, i.qty, i.box_label, i.expiry) for i in items]
            result = attach_picked_qty(session, supply_id, opis_tuples)
            transition(session, supply_id, "picked", event=f"opis_wb {stored_path.name}")
            await cb.answer("Привязано")
            if cb.message:
                miss = result["missing"]
                miss_txt = f"\nНе нашёл в поставке: {', '.join(miss)}" if miss else ""
                await safe_edit_or_answer(cb.message,
                    f"📎 Привязал опись WB к поставке #{supply_id}. "
                    f"Совпало: {result['matched']}/{len(opis_tuples)}.{miss_txt}\n"
                    f"Состояние → picked"
                )

        elif kind == FileKind.OPIS_OZON.value:
            items = parse_opis_ozon(stored_path)
            opis_tuples = [(i.barcode, i.qty, i.box_label, i.expiry) for i in items]
            result = attach_picked_qty(session, supply_id, opis_tuples)
            transition(session, supply_id, "picked", event=f"opis_ozon {stored_path.name}")
            await cb.answer("Привязано")
            if cb.message:
                miss = result["missing"]
                miss_txt = f"\nНе нашёл в поставке: {', '.join(miss)}" if miss else ""
                await safe_edit_or_answer(cb.message,
                    f"📎 Привязал опись Озон к поставке #{supply_id}. "
                    f"Совпало: {result['matched']}/{len(opis_tuples)}.{miss_txt}\n"
                    f"Состояние → picked"
                )

        elif kind == FileKind.PRIHOD.value:
            doc = parse_prihod(stored_path)
            prihod_tuples = [(i.article, i.qty) for i in doc.items]
            discrep = reconcile_prihod(session, supply_id, prihod_tuples)
            transition(session, supply_id, "intake_done",
                       event=f"prihod {doc.doc_number or stored_path.name}")
            lines = [
                f"📎 Приходная привязана к поставке #{supply_id}. Состояние → intake_done"
            ]
            if discrep:
                lines.append(f"\n⚠ Расхождений: {len(discrep)}")
                for d in discrep[:20]:
                    sign = "+" if d.delta > 0 else ""
                    lines.append(f"  <code>{d.article}</code>: план {d.expected}, факт {d.actual} ({sign}{d.delta})")
            else:
                lines.append("\n✅ Расхождений нет.")
            await cb.answer("Сверено")
            if cb.message:
                await safe_edit_or_answer(cb.message,"\n".join(lines))

        elif kind == FileKind.OSTATKI.value:
            items = parse_ostatki(stored_path)
            await cb.answer()
            lines = [f"📎 Остатки от ФФ привязал к контексту поставки #{supply_id}.",
                     f"Записей: {len(items)}"]
            top = sorted(items, key=lambda x: -x.balance)[:10]
            for i in top:
                lines.append(f"  <code>{i.article}</code>: {i.balance} (дост {i.available} / рез {i.reserved})")
            if cb.message:
                await safe_edit_or_answer(cb.message,"\n".join(lines))


def _parse(path: Path, kind: FileKind) -> tuple[str, dict]:
    if kind == FileKind.OPIS_WB:
        items = parse_opis_wb(path)
        summary = f"Опись WB: {len(items)} позиций, ШК короб {items[0].box_label if items else '?'}"
        payload = [
            {"barcode": i.barcode, "qty": i.qty, "box": i.box_label, "exp": str(i.expiry) if i.expiry else None}
            for i in items
        ]
        return summary, {"items": payload}

    if kind == FileKind.OPIS_OZON:
        items = parse_opis_ozon(path)
        summary = f"Опись Озон: {len(items)} позиций"
        payload = [
            {"barcode": i.barcode, "article": i.article, "qty": i.qty,
             "box": i.box_label, "exp": str(i.expiry) if i.expiry else None}
            for i in items
        ]
        return summary, {"items": payload}

    if kind == FileKind.PRIHOD:
        doc = parse_prihod(path)
        summary = f"Приходная № {doc.doc_number or '?'}: {len(doc.items)} позиций"
        payload = {
            "doc_number": doc.doc_number,
            "items": [{"article": i.article, "qty": i.qty, "total": i.total} for i in doc.items],
        }
        return summary, payload

    if kind == FileKind.OSTATKI:
        items = parse_ostatki(path)
        summary = f"Остатки: {len(items)} SKU"
        payload = {
            "items": [{"article": i.article, "balance": i.balance,
                       "available": i.available, "reserved": i.reserved}
                      for i in items]
        }
        return summary, payload

    return "?", {}


def _save_inbox(path: Path, original_name: str, kind: str, supply_id, payload: dict) -> int:
    with db_session() as session:
        inbox = InboxFile(
            original_name=original_name,
            file_kind=kind,
            supply_id=supply_id,
            parsed_payload_json=payload,
            file_path=str(path),
        )
        session.add(inbox)
        session.flush()
        return inbox.id
