"""Слот-хантер: разведка доступных слотов для заявки на отгрузку.

Этап 3a: ТОЛЬКО разведка (read-only). Возвращает кандидатов на бронирование.
Этап 3b: реальное бронирование через API — отдельная функция book_*, защищена подтверждением.

Безопасность WB: используем ТОЛЬКО официальные эндпоинты (/api/v1/acceptance/coefficients,
/api/v3/supplies). Без поллинга чаще 1 раз/мин.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

from src.integrations import OzonClient, OzonAPIError, WBClient, WBAPIError
from src.warehouses import WB_CLUSTERS, OZON_CLUSTERS

logger = logging.getLogger("services.slot_hunter")


@dataclass
class SlotCandidate:
    """Один кандидат на бронирование слота."""
    marketplace: str         # 'wb' | 'ozon'
    cluster: str             # имя кластера из заявки
    warehouse_name: str      # реальное имя склада МП
    warehouse_id: Optional[int] = None
    slot_date: Optional[date] = None
    coefficient: Optional[float] = None    # WB: 0=бесплатно, >0=платно
    box_type: Optional[str] = None         # WB: 'Короба'/'Монопаллеты'
    delivery_coef: Optional[float] = None  # WB: 1.0 = 100% базовый тариф логистики
    available: bool = True


def _normalize(s: str) -> str:
    """Нормализация имени для fuzzy-матчинга.
    Разделители (-,_,/) → пробелы, остальное удаляем, ё→е, lowercase.
    """
    s = (s or "").lower().replace("ё", "е")
    s = re.sub(r"[-_,/]+", " ", s)
    s = re.sub(r"[^а-яa-z0-9 ]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _wb_cluster_to_warehouses(cluster_name: str) -> List[str]:
    """Какие WB-склады относятся к нашему кластеру?
    Сначала точное имя, потом fuzzy по подстроке.
    """
    if cluster_name in WB_CLUSTERS:
        return WB_CLUSTERS[cluster_name]
    norm = _normalize(cluster_name)
    for k, v in WB_CLUSTERS.items():
        if _normalize(k) == norm or norm in _normalize(k) or _normalize(k) in norm:
            return v
    # Fallback: эвристика по ключевым словам
    keywords = {
        "центр": "Центральный (МСК)",
        "сибир": "Сибирский / ДВ",
        "юж": "Южный",
        "приволж": "Приволжский",
        "урал": "Уральский (ЕКБ)",
        "северо-зап": "Северо-Западный (СПБ)",
        "северо-кав": "Южный",
        "снг": "СНГ",
    }
    for kw, cluster in keywords.items():
        if kw in norm:
            return WB_CLUSTERS.get(cluster, [])
    return []


def _ozon_cluster_to_name(cluster_name: str) -> Optional[str]:
    """Сопоставить имя кластера из файла Ozon с ключом в OZON_CLUSTERS."""
    if cluster_name in OZON_CLUSTERS:
        return cluster_name
    norm = _normalize(cluster_name)
    for k in OZON_CLUSTERS:
        if _normalize(k) == norm or norm in _normalize(k) or _normalize(k) in norm:
            return k
    keywords = {
        "москв": "Москва и МО",
        "питер": "Санкт-Петербург",
        "санкт": "Санкт-Петербург",
        "спб": "Санкт-Петербург",
        "юг": "Юг",
        "ростов": "Юг",
        "сибир": "Сибирь",
        "урал": "Урал",
        "повол": "Поволжье",
        "дальн": "Дальний Восток",
    }
    for kw, ck in keywords.items():
        if kw in norm:
            return ck
    return None


async def hunt_wb(
    wb: WBClient,
    cluster_name: str,
    target_dates: List[date],
    max_coef: int = 5,
    goods: Optional[List[Dict[str, int]]] = None,
) -> Tuple[List[SlotCandidate], List[str]]:
    """Найти доступные слоты WB для кластера.

    goods: [{"barcode": str, "quantity": int}, ...] — если задано, фильтруем склады
    по тем, что готовы принять ВСЕ эти баркоды (через /api/v1/acceptance/options).

    Возвращает (candidates, warnings).
    """
    warnings: List[str] = []
    candidates: List[SlotCandidate] = []

    # 1. Какие склады относятся к нашему кластеру по нашему мэппингу
    target_warehouse_names = _wb_cluster_to_warehouses(cluster_name)
    if not target_warehouse_names:
        warnings.append(f"WB: не нашёл маппинг кластера «{cluster_name}» — пропускаю.")
        return candidates, warnings

    target_norms = {_normalize(n) for n in target_warehouse_names}

    # 2. Реальный список WB-складов через API (с файловым кэшем + retry)
    try:
        all_wbs = await wb.warehouses()
    except WBAPIError as e:
        warnings.append(
            f"WB API warehouses: {str(e)[:200]}\n"
            f"💡 Запусти /api_warmup чтобы прогреть файловый кэш (24ч)."
        )
        return candidates, warnings

    # Маппинг: id → имя для тех что попали в наш кластер.
    # Строгий матчинг: ВСЕ слова таргета должны быть в имени API склада
    # (иначе «Рязань Тюшевское» (не-food) матчился с food-таргетом «Рязань Тюшевское Питание»).
    wb_id_to_name: Dict[int, str] = {}
    target_word_sets = [set(tn.split()) for tn in target_norms if tn]
    for w in all_wbs:
        name = w.get("name") or w.get("warehouseName") or ""
        wid = w.get("ID") or w.get("id") or w.get("warehouseId")
        if not wid:
            continue
        n = _normalize(name)
        n_words = set(n.split())
        for tw in target_word_sets:
            if tw and tw.issubset(n_words):
                wb_id_to_name[int(wid)] = name
                break

    if not wb_id_to_name:
        warnings.append(f"WB: API вернул склады, но ни один не совпал с кластером «{cluster_name}».")
        return candidates, warnings

    # 2b. Фильтр по acceptance/options — какие склады РЕАЛЬНО принимают эти товары
    if goods:
        try:
            opts = await wb.acceptance_options(goods)
        except WBAPIError as e:
            warnings.append(f"WB acceptance_options: {str(e)[:200]} — пропускаю фильтр по товарам")
        else:
            # Пересечение: warehouse-ids которые принимают КАЖДЫЙ баркод
            per_bc_ids: List[set] = []
            errors_bc: List[str] = []
            for item in opts:
                bc = item.get("barcode")
                if item.get("isError") or not item.get("warehouses"):
                    errors_bc.append(str(bc))
                    continue
                whs = {int(w.get("warehouseID")) for w in item["warehouses"] if w.get("warehouseID")}
                per_bc_ids.append(whs)
            if errors_bc:
                warnings.append(f"WB acceptance: эти баркоды не нашлись/ошибка: {', '.join(errors_bc[:5])}")
            if per_bc_ids:
                accepted_ids = set.intersection(*per_bc_ids) if len(per_bc_ids) > 1 else per_bc_ids[0]
                before = len(wb_id_to_name)
                wb_id_to_name = {wid: n for wid, n in wb_id_to_name.items() if wid in accepted_ids}
                if not wb_id_to_name:
                    warnings.append(
                        f"WB: ни один склад кластера «{cluster_name}» не готов принять весь набор "
                        f"({before} складов отфильтрованы acceptance/options)."
                    )
                    return candidates, warnings

    # 3. Коэффициенты приёмки
    try:
        coefs = await wb.acceptance_coefficients(warehouse_ids=list(wb_id_to_name.keys()))
    except WBAPIError as e:
        warnings.append(f"WB API coefficients: {str(e)[:200]}")
        return candidates, warnings

    target_dates_iso = {d.isoformat() for d in target_dates}

    for c in coefs:
        coef = c.get("coefficient")
        if coef is None or coef < 0 or coef > max_coef:
            continue
        date_s = (c.get("date") or "")[:10]
        if target_dates_iso and date_s not in target_dates_iso:
            continue
        wid = c.get("warehouseID") or c.get("warehouseId")
        if wid is None:
            continue
        wid = int(wid)
        if wid not in wb_id_to_name:
            continue
        try:
            slot_d = date.fromisoformat(date_s)
        except ValueError:
            continue
        try:
            dlv = float(c.get("deliveryCoef") or 0)
        except (ValueError, TypeError):
            dlv = 0.0
        candidates.append(SlotCandidate(
            marketplace="wb",
            cluster=cluster_name,
            warehouse_name=wb_id_to_name[wid],
            warehouse_id=wid,
            slot_date=slot_d,
            coefficient=float(coef),
            box_type=c.get("boxTypeName"),
            delivery_coef=dlv,
        ))

    # Сортировка: дешевле логистика → дешевле приёмка → раньше дата
    candidates.sort(key=lambda x: (x.delivery_coef or 99, x.coefficient or 99, x.slot_date or date.max))
    return candidates, warnings


async def hunt_ozon(
    oz: OzonClient,
    cluster_name: str,
    target_dates: List[date],
) -> Tuple[List[SlotCandidate], List[str]]:
    """Найти доступные склады Ozon для кластера.

    Ozon: не отдаёт коэффициенты напрямую через cluster_list. Доступность слотов
    проверяется через /v1/draft/timeslot/info (требует draft_id). Здесь делаем
    только структурную разведку: какие склады в кластере доступны.
    """
    warnings: List[str] = []
    candidates: List[SlotCandidate] = []

    matched_key = _ozon_cluster_to_name(cluster_name)
    if not matched_key:
        warnings.append(f"Ozon: не нашёл локальный кластер «{cluster_name}» — пропускаю.")
        return candidates, warnings

    try:
        api_clusters = await oz.cluster_list()
    except OzonAPIError as e:
        warnings.append(f"Ozon API cluster_list: {str(e)[:200]}")
        # Fallback: используем наш статичный список
        for w in OZON_CLUSTERS.get(matched_key, []):
            candidates.append(SlotCandidate(
                marketplace="ozon",
                cluster=cluster_name,
                warehouse_name=w,
            ))
        return candidates, warnings

    target_norm = _normalize(matched_key)
    for cl in api_clusters:
        cname = cl.get("name") or ""
        if _normalize(cname) != target_norm and target_norm not in _normalize(cname):
            continue
        for lc in cl.get("logistic_clusters") or []:
            for w in lc.get("warehouses") or []:
                wname = w.get("name") or ""
                wid = w.get("warehouse_id")
                candidates.append(SlotCandidate(
                    marketplace="ozon",
                    cluster=cluster_name,
                    warehouse_name=wname,
                    warehouse_id=int(wid) if wid else None,
                ))
        break

    if not candidates:
        # Если API нашёл кластер но без складов — fallback
        for w in OZON_CLUSTERS.get(matched_key, []):
            candidates.append(SlotCandidate(
                marketplace="ozon",
                cluster=cluster_name,
                warehouse_name=w,
            ))

    return candidates, warnings
