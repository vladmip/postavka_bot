"""Создание поставки FBO Ozon через Draft API.

Workflow:
  1. /ozon_book <ship_id> — стартует
  2. Бот собирает items по Ozon-кластерам из заявки
  3. Спрашивает тип (CROSSDOCK / DIRECT)
  4. POST /v1/draft/create
  5. Polls /v1/draft/create/info до status=DONE
  6. POST /v1/draft/timeslot/info — показывает доступные drop-off и слоты
  7. Пользователь тапает слот → POST /v1/draft/supply/create
  8. Polls /v1/draft/supply/create/info → supply создана в Ozon ЛК
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from aiogram import Router, F
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from src.bot.helpers import safe_edit_or_answer, send_long
from src.config import APIKEY_OZON, CLIENT_ID_OZON, OZON_PROXY_URL
from src.db.models import ShipmentRequest, ShipmentItem, Sku
from src.db.session import db_session
from src.integrations import OzonClient, OzonAPIError
from src.services.shipment_service import get_shipment_request
from src.services.slot_hunter import _ozon_cluster_to_name, _normalize

router = Router()
logger = logging.getLogger("bot.ozon_book")


class OzonBook(StatesGroup):
    pick_type = State()
    pick_slot = State()


# ── helpers ─────────────────────────────────────────────────────────────────


def _build_items_for_cluster(req: ShipmentRequest, cluster: str) -> Tuple[List[Dict], List[str]]:
    """Собрать items для draft из заявки.
    Возвращает (items, missing_articles).
    items: [{"sku": int, "quantity": int}]  ← Ozon API требует sku (числовой product_id).
    """
    items: List[Dict] = []
    missing: List[str] = []
    by_sku: Dict[int, int] = {}
    for it in req.items:
        if it.marketplace != "ozon" or it.cluster != cluster:
            continue
        sku = it.sku
        if not sku or not sku.ozon_sku:
            missing.append(it.raw_article)
            continue
        by_sku[sku.ozon_sku] = by_sku.get(sku.ozon_sku, 0) + it.qty
    for ozon_sku, qty in by_sku.items():
        items.append({"sku": ozon_sku, "quantity": qty})
    return items, missing


async def _resolve_ozon_cluster_id(oz: OzonClient, cluster_name_local: str) -> Optional[int]:
    """Найти macrolocal_cluster_id у Ozon API по нашему имени кластера.
    С 03.2026 новые endpoint draft/*/create требуют именно macrolocal_cluster_id,
    а не старый cluster.id из cluster_list.
    """
    matched_key = _ozon_cluster_to_name(cluster_name_local)
    if not matched_key:
        return None
    try:
        clusters = await oz.cluster_list()
    except OzonAPIError as e:
        logger.warning("cluster_list failed: %s", e)
        return None
    target_norm = _normalize(matched_key)
    target_norm_local = _normalize(cluster_name_local)
    for cl in clusters:
        cname = cl.get("name") or ""
        n = _normalize(cname)
        if n == target_norm or n == target_norm_local or target_norm in n or n in target_norm:
            mcid = cl.get("macrolocal_cluster_id") or cl.get("id")
            try:
                return int(mcid)
            except (ValueError, TypeError):
                return None
    return None


async def _wait_draft_ready(oz: OzonClient, op_id: str, max_attempts: int = 30) -> Dict:
    """Polls /v1/draft/create/info до status DONE/FAILED. Пауза 3с между попытками."""
    for i in range(max_attempts):
        await asyncio.sleep(3 if i > 0 else 2)  # пауза ДО запроса (после предыдущего)
        try:
            info = await oz.draft_create_info(op_id)
        except OzonAPIError as e:
            if "429" in str(e):
                # ретрай уже внутри клиента, если всё равно 429 — ждём дольше
                await asyncio.sleep(5)
                continue
            raise
        status = info.get("status", "")
        if status in {"CALCULATION_STATUS_SUCCESS", "STATUS_DONE", "DONE", "SUCCESS"}:
            return info
        if "FAIL" in status.upper():
            return info
    return {"status": "TIMEOUT"}


# ── /ozon_book — wizard ─────────────────────────────────────────────────────

@router.message(Command("ozon_book"))
async def cmd_ozon_book(msg: Message, command: CommandObject, state: FSMContext) -> None:
    try:
        rid = int((command.args or "").strip())
    except ValueError:
        await msg.answer("Использование: <code>/ozon_book ID</code>")
        return
    await _start_ozon_book_wizard(msg, state, rid)


@router.callback_query(F.data.startswith("ozon_book_card:"))
async def cb_ozon_book_from_card(cb: CallbackQuery, state: FSMContext) -> None:
    """Триггер /ozon_book из карточки заявки."""
    rid = int(cb.data.split(":", 1)[1])
    await cb.answer("Запускаю мастер Ozon…")
    if cb.message:
        await _start_ozon_book_wizard(cb.message, state, rid)


async def _start_ozon_book_wizard(msg: Message, state: FSMContext, rid: int) -> None:
    if not APIKEY_OZON or not CLIENT_ID_OZON:
        await msg.answer("⚠ Ozon-ключи не заданы. /api_check для проверки.")
        return

    # Собираем Ozon-направления заявки
    summaries: List[Tuple[str, int, int, List[str]]] = []  # (cluster, n_items, total_qty, missing)
    with db_session() as session:
        req = get_shipment_request(session, rid)
        if not req:
            await msg.answer(f"Заявка #{rid} не найдена.")
            return
        if not req.target_date_from:
            await msg.answer(
                f"⚠ У заявки #{rid} нет целевых дат. Сначала /ship_plan."
            )
            return

        oz_clusters = sorted({it.cluster for it in req.items if it.marketplace == "ozon"})
        for cl in oz_clusters:
            items, missing = _build_items_for_cluster(req, cl)
            total_qty = sum(it["quantity"] for it in items)
            summaries.append((cl, len(items), total_qty, missing))
        date_from = req.target_date_from.date().isoformat()
        date_to = (req.target_date_to or req.target_date_from).date().isoformat()

    if not summaries:
        await msg.answer(f"В заявке #{rid} нет Ozon-направлений.")
        return

    lines = [f"📦 <b>Создание Ozon-поставок для заявки #{rid}</b>\n"]
    has_missing = False
    for cl, n_items, total_qty, missing in summaries:
        lines.append(f"<b>«{cl}»</b>: {n_items} SKU, {total_qty} шт")
        if missing:
            has_missing = True
            lines.append(f"  ⚠ Без offer_id ({len(missing)}): {', '.join(missing[:5])}")
    lines.append(f"\nДаты: {date_from} — {date_to}")

    if has_missing:
        lines.append(
            "\n💡 Запусти /sku_link_ozon чтобы привязать недостающие SKU к Ozon offer_id + sku."
        )

    # Сохраним state и сразу запустим creation (всегда DIRECT, без выбора типа)
    await state.update_data(
        ob_rid=rid,
        ob_clusters=[s[0] for s in summaries],
        ob_date_from=date_from,
        ob_date_to=date_to,
        ob_type="CREATE_TYPE_DIRECT",
    )
    lines.append("\n⏳ Создаю draft (DIRECT)…")
    await msg.answer("\n".join(lines))
    await _create_drafts(msg, state)


@router.callback_query(OzonBook.pick_type, F.data.startswith("obtype:"))
async def cb_ob_type(cb: CallbackQuery, state: FSMContext) -> None:
    mode = cb.data.split(":", 1)[1]
    draft_type = "CREATE_TYPE_CROSSDOCK" if mode == "cross" else "CREATE_TYPE_DIRECT"
    await state.update_data(ob_type=draft_type)
    await cb.answer("Создаю draft в Ozon…")
    if cb.message:
        info_txt = (
            f"⏳ Создаю Ozon draft (type={draft_type})…\n"
            "Запрос API, polling статуса (~5-30 сек).\n"
        )
        if draft_type == "CREATE_TYPE_CROSSDOCK":
            info_txt += (
                "\n💡 <b>Drop-off склад</b> выберешь после: Ozon вернёт список доступных "
                "приёмочных пунктов МСК со слотами — тапнешь нужный."
            )
        await safe_edit_or_answer(cb.message, info_txt)
        await _create_drafts(cb.message, state)


async def _create_drafts(msg: Message, state: FSMContext) -> None:
    data = await state.get_data()
    rid = data["ob_rid"]
    clusters = data["ob_clusters"]
    draft_type = data["ob_type"]
    date_from = data["ob_date_from"]
    date_to = data["ob_date_to"]

    oz = OzonClient(CLIENT_ID_OZON, APIKEY_OZON, proxy=OZON_PROXY_URL)

    # Под каждый кластер — отдельный draft
    drafts_made: List[Dict] = []
    for cl in clusters:
        await msg.answer(f"🔄 Кластер <b>«{cl}»</b>…")

        # Резолвим cluster_id
        try:
            cid = await _resolve_ozon_cluster_id(oz, cl)
        except OzonAPIError as e:
            await msg.answer(f"⚠ cluster_list: <code>{str(e)[:200]}</code>")
            continue
        if not cid:
            await msg.answer(f"⚠ Не сматчил «{cl}» с Ozon-кластером. Пропускаю.")
            continue

        # Собираем items
        with db_session() as session:
            req = get_shipment_request(session, rid)
            if not req:
                await msg.answer(f"Заявка #{rid} пропала.")
                return
            items, missing = _build_items_for_cluster(req, cl)

        if not items:
            await msg.answer(f"⚠ «{cl}»: нет SKU с offer_id — нечего бронировать.")
            continue

        await msg.answer(
            f"  POST /v1/draft/create: cluster_id={cid}, items={len(items)}… "
            "(жду 15 сек чтобы не упереться в method-specific rate-limit)"
        )
        # Ozon rate-limit жёсткий на draft/create — пауза побольше
        await asyncio.sleep(15.0)
        try:
            op_id = await oz.draft_create(
                items=items, cluster_ids=[cid], draft_type=draft_type,
            )
        except OzonAPIError as e:
            await msg.answer(f"❌ draft_create: <code>{str(e)[:400]}</code>")
            continue

        # Новые endpoints (03.2026) синхронные — возвращают "sync:<draft_id>"
        # Старые async-endpoints возвращают operation_id для polling.
        if op_id.startswith("sync:"):
            draft_id = op_id.split(":", 1)[1]
        else:
            await msg.answer(f"  ⏳ operation_id={op_id[:24]}…, polling…")
            info = await _wait_draft_ready(oz, op_id)
            status = info.get("status", "?")
            if "SUCCESS" not in status.upper() and "DONE" not in status.upper():
                errs = info.get("errors") or []
                err_s = "; ".join(str(e)[:120] for e in errs[:3]) if errs else "?"
                await msg.answer(
                    f"❌ draft не готов: status={status}\nerrors: <code>{err_s}</code>"
                )
                continue
            draft_id = info.get("draft_id") or info.get("calculation_id")
        if not draft_id:
            await msg.answer("⚠ Нет draft_id в ответе.")
            continue

        drafts_made.append({
            "cluster": cl,
            "cluster_id": cid,
            "draft_id": int(draft_id),
            "operation_id": op_id,
            "items_count": len(items),
        })
        await msg.answer(f"  ✅ draft_id=<code>{draft_id}</code>")

    if not drafts_made:
        await msg.answer("⚠ Ни один draft не создан.")
        await state.clear()
        return

    await state.update_data(ob_drafts=drafts_made)

    # Тянем таймслоты для каждого draft
    date_from_iso = f"{date_from}T00:00:00Z"
    date_to_iso = f"{date_to}T23:59:59Z"

    all_buttons: List[List[InlineKeyboardButton]] = []
    slot_counter = 0

    for d in drafts_made:
        await msg.answer(
            f"📅 Таймслоты для draft #{d['draft_id']} ({d['cluster']})…\n"
            "<i>(глобальный лимит 2 req/sec на всех продавцов — может занять до 30 сек)</i>"
        )
        await asyncio.sleep(5.0)  # подальше от draft_create, рядом с global rate-limit
        try:
            ts = await oz.draft_timeslot_info(
                draft_id=d["draft_id"],
                date_from=date_from_iso,
                date_to=date_to_iso,
            )
        except OzonAPIError as e:
            await msg.answer(
                f"⚠ timeslot/info: <code>{str(e)[:300]}</code>\n"
                "💡 Черновик создан в Ozon ЛК — можешь дойти руками: "
                "FBO → Поставки → Черновики."
            )
            continue

        wh_timeslots = ts.get("drop_off_warehouse_timeslots") or []
        if not wh_timeslots:
            await msg.answer(f"🔴 Слотов нет для «{d['cluster']}» в эти даты.")
            continue

        lines = [f"🟢 <b>{d['cluster']}</b> — {len(wh_timeslots)} drop-off"]
        for wh in wh_timeslots[:5]:
            wh_id = wh.get("drop_off_warehouse_id") or wh.get("warehouse_id")
            wh_name = wh.get("warehouse_name") or f"#{wh_id}"
            days = wh.get("days") or []
            if not days:
                continue
            lines.append(f"\n<b>{wh_name}</b>")
            for day in days[:3]:
                date_s = day.get("date_in_timezone") or day.get("date") or "?"
                date_short = date_s[:10]
                slots = day.get("timeslots") or []
                for slot in slots[:2]:
                    t_from = slot.get("from_in_timezone") or slot.get("from") or ""
                    t_to = slot.get("to_in_timezone") or slot.get("to") or ""
                    # Кнопка
                    slot_counter += 1
                    btn_label = f"📌 {date_short} {t_from[11:16]} {wh_name[:12]}"
                    cb_data = f"obslot:{slot_counter}"
                    all_buttons.append([InlineKeyboardButton(text=btn_label[:40], callback_data=cb_data)])
                    # Сохраняем подробности в state под этим ключом
                    await state.update_data(**{
                        f"slot_{slot_counter}": {
                            "draft_id": d["draft_id"],
                            "warehouse_id": int(wh_id),
                            "warehouse_name": wh_name,
                            "from": t_from,
                            "to": t_to,
                            "cluster": d["cluster"],
                        }
                    })
                    lines.append(f"  {date_short} {t_from[11:16]}–{t_to[11:16]}")
        await send_long(msg, "\n".join(lines))

    if not all_buttons:
        await msg.answer("⚠ Подходящих слотов не нашлось во всех drafts.")
        await state.clear()
        return

    all_buttons.append([InlineKeyboardButton(text="✖ Отмена", callback_data="cancel")])
    await state.set_state(OzonBook.pick_slot)
    await msg.answer(
        f"✅ Найдено {slot_counter} слотов. Выбери:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=all_buttons[:30]),
    )


@router.callback_query(OzonBook.pick_slot, F.data.startswith("obslot:"))
async def cb_ob_slot_pick(cb: CallbackQuery, state: FSMContext) -> None:
    slot_n = int(cb.data.split(":", 1)[1])
    data = await state.get_data()
    slot = data.get(f"slot_{slot_n}")
    if not slot:
        await cb.answer("Слот пропал — повтори /ozon_book", show_alert=True)
        return

    rid = data["ob_rid"]
    await state.clear()
    await cb.answer("Бронирую…")

    oz = OzonClient(CLIENT_ID_OZON, APIKEY_OZON, proxy=OZON_PROXY_URL)
    if cb.message:
        await safe_edit_or_answer(
            cb.message,
            f"⏳ POST /v1/draft/supply/create\n"
            f"draft_id={slot['draft_id']}\n"
            f"warehouse={slot['warehouse_name']} (id={slot['warehouse_id']})\n"
            f"timeslot={slot['from'][:16]} — {slot['to'][:16]}"
        )
    try:
        op_id = await oz.draft_supply_create(
            draft_id=slot["draft_id"],
            timeslot_from=slot["from"],
            timeslot_to=slot["to"],
            warehouse_id=slot["warehouse_id"],
        )
    except OzonAPIError as e:
        if cb.message:
            await cb.message.answer(f"❌ Ошибка бронирования: <code>{str(e)[:400]}</code>")
        return

    if cb.message:
        await cb.message.answer(f"⏳ operation_id={op_id[:24]}… polling финализации")

    # Polls supply/create/info
    final = None
    for _ in range(30):
        await asyncio.sleep(2)
        try:
            info = await oz.draft_supply_create_info(op_id)
        except OzonAPIError as e:
            if cb.message:
                await cb.message.answer(f"⚠ create_info: <code>{str(e)[:200]}</code>")
            return
        status = info.get("status", "")
        if "SUCCESS" in status.upper() or "DONE" in status.upper():
            final = info
            break
        if "FAIL" in status.upper():
            final = info
            break

    if not final:
        if cb.message:
            await cb.message.answer("⚠ Таймаут на финализации supply (но в ЛК может появиться).")
        return

    status = final.get("status", "?")
    success = "SUCCESS" in status.upper() or "DONE" in status.upper()
    supply_ids = final.get("supply_ids") or final.get("supplies") or []

    if cb.message:
        if success:
            sids = ", ".join(str(s) for s in supply_ids) if supply_ids else "?"
            await cb.message.answer(
                f"✅ <b>Поставка создана в Ozon ЛК!</b>\n"
                f"supply_id: <code>{sids}</code>\n"
                f"Кластер: {slot['cluster']}\n"
                f"Drop-off: {slot['warehouse_name']}\n"
                f"Слот: {slot['from'][:16]} — {slot['to'][:16]}\n\n"
                f"Проверь в Ozon ЛК → FBO → Поставки."
            )
            # Запомним в БД
            with db_session() as session:
                req = get_shipment_request(session, rid)
                if req:
                    if supply_ids:
                        for it in req.items:
                            if it.marketplace == "ozon" and it.cluster == slot["cluster"]:
                                it.booked_supply_id = str(supply_ids[0])
                                it.target_warehouse = slot["warehouse_name"]
                                it.booked_slot_at = datetime.fromisoformat(slot["from"].replace("Z", "+00:00").split("+")[0])
                    req.state = "supplies_created"
        else:
            errs = final.get("errors") or []
            err_s = "; ".join(str(e)[:100] for e in errs[:3])
            await cb.message.answer(
                f"❌ status={status}\nerrors: <code>{err_s}</code>"
            )
