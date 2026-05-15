# Ozon Seller API — какие методы нужны боту

Полный список endpoint'ов которые дёргает Postavka Assistant Bot. Разбито по фичам, с указанием раздела API-ключа.

**Если у юзера ключ с ограниченными правами — какие-то фичи не будут работать.** Минимум для бота — Admin-ключ (полные права). Если хочется ограничить scope — внизу таблица «минимальные права по разделам».

---

## 1. Онбординг + проверка ключа

| Endpoint | Метод | Зачем |
|---|---|---|
| `POST /v3/product/list` | `product_list(limit=1)` | Тестовый запрос при вводе API key (validate_ozon_creds). 401/403 → ключи не подходят. |

**Раздел:** Каталог товаров (read).

---

## 2. Sync каталога (онбординг + утренняя рутина + кнопка «Обновить каталог»)

| Endpoint | Метод | Зачем |
|---|---|---|
| `POST /v3/product/list` | `product_list(limit=5000)` | Все товары: offer_id + product_id, с пагинацией. |
| `POST /v3/product/info/list` | `product_info_list(product_ids)` | Детали (name, baroce, sku) пачкой по 500. |

**Раздел:** Каталог товаров (read).

---

## 3. Остатки FBO (digest + карточка заявки + диагностика)

| Endpoint | Метод | Зачем |
|---|---|---|
| `POST /v4/product/info/stocks` | `stocks_fbo(limit=1000)` | Live остатки FBO по складам (cursor-based pagination). |

**Раздел:** Остатки на складах (Stocks/FBO read).

---

## 4. Заказы FBO (digest — расчёт rate of sale)

| Endpoint | Метод | Зачем |
|---|---|---|
| `POST /v3/posting/fbo/list` | `postings_fbo_list(date_from, date_to)` | Заказы FBO за 28 дней (cursor + status[]). Используется для urgent / runout SKU в digest. |

**Раздел:** Заказы FBO (read).

⚠ v2 deprecated с 01.06.2026 — сейчас на v3.

---

## 5. Возвраты (digest + меню «📥 Возвраты»)

### 5.1 Покупательские возвраты (товар вернул клиент)

| Endpoint | Метод | Зачем |
|---|---|---|
| `POST /v1/returns/list` | `returns_list(filter, limit=500)` | Список возвратов FBO + FBS со статусами (Arrived at place / In transit / Received…). |

### 5.2 Партии вывоза (giveouts) — упаковки которые продавцу везут в ПВЗ

| Endpoint | Метод | Зачем |
|---|---|---|
| `POST /v1/return/giveout/is-enabled` | `returns_giveout_is_enabled()` | Доступен ли вообще giveout-флоу для продавца. |
| `POST /v1/return/giveout/list` | `returns_giveout_list(limit=200)` | Партии CREATED/APPROVED/COMPLETED — счётчик в digest. |
| `POST /v1/return/giveout/info` | `returns_giveout_info(giveout_id)` | Состав конкретной партии. |
| `POST /v1/return/giveout/get-pdf` | `returns_giveout_get_pdf()` | PDF этикетки для получения партии. |

### 5.3 Removal — детальный список вывозов товара продавцу (главный источник для digest)

| Endpoint | Метод | Зачем |
|---|---|---|
| `POST /v1/removal/from-stock/list` | `removal_from_stock_list(date_from, date_to)` | Вывозы СО СТОКА FBO (товар лежал на складе, продавец заказал вывезти). Раздел в ЛК: `?tab=Stock`. |
| `POST /v1/removal/from-supply/list` | `removal_from_supply_list(date_from, date_to)` | Вывозы С ПОСТАВКИ (Ozon отбраковал на приёмке, возвращает). Раздел в ЛК: `?tab=Supply`. |

Поля ответа: `name, offer_id, sku, box_id, return_id, return_state, destination_warehouse_name, destination_warehouse_address, delivery_date, given_out_date, utilization_date, quantity_for_return`.

В digest группируем по складу + статусу, показываем 🔴 «в ПВЗ» / 🟡 «в пути», адрес ПВЗ и пару артикулов.

**Раздел:** Возвраты (read) + FBO Поставки (read для removal/*).

---

## 6. Кластеры и склады FBO (поиск drop-off, выбор кластера в wizard)

| Endpoint | Метод | Зачем |
|---|---|---|
| `POST /v1/cluster/list` | `cluster_list()` | Список FBO-кластеров (Москва, СПб, ...) со складами. Кэшируется 24ч. |
| `POST /v1/warehouse/fbo/list` | `warehouse_fbo_list()` | Список FBO-складов (без кластерной разбивки). |
| `POST /v1/warehouse/fbs/create/drop-off/list` | `warehouse_fbs_drop_off_list(...)` | Поиск drop-off точек (ПВЗ/ППЗ/СЦ) по адресу для кроссдока. |
| `POST /v1/warehouse/list` | `warehouse_list()` | Список FBS-складов поставщика (если есть). |

**Раздел:** Склады (Warehouse read).

---

## 7. Создание поставки Ozon FBO (главный flow в wizard'е ozon_book)

### 7.1 Создание draft

| Endpoint | Метод | Зачем |
|---|---|---|
| `POST /v1/draft/direct/create` | `draft_create(draft_type=DIRECT)` | Direct-поставка: товары едут напрямую на конкретный РФЦ. |
| `POST /v1/draft/crossdock/create` | `draft_create(draft_type=CROSSDOCK)` | CROSSDOCK: товары на drop-off хаб, Ozon развозит. |
| `POST /v1/draft/multi-cluster/create` | `draft_create(cluster_ids=[N+])` | Если кластеров несколько в одной заявке. |

### 7.2 Получение scoring (доступные склады)

| Endpoint | Метод | Зачем |
|---|---|---|
| `POST /v2/draft/create/info` | `draft_create_info(draft_id)` | Sync v2 (после 16.03.2026) — полный scoring складов. |
| `POST /v1/draft/create/info` | `draft_create_info(operation_id)` | Legacy v1 — для асинхронных drafts. |

### 7.3 Получение слотов

| Endpoint | Метод | Зачем |
|---|---|---|
| `POST /v2/draft/timeslot/info` | `draft_timeslot_info(draft_id, ...)` | Свободные таймслоты на склад/дату. **Глобальный rate-limit 2/сек** — бот делает осторожный backoff. |

### 7.4 Финализация поставки → создаёт реальный supply_order

| Endpoint | Метод | Зачем |
|---|---|---|
| `POST /v2/draft/supply/create` | `draft_supply_create_v2(...)` | Создать поставку (после 16.03.2026 — новый sync API). |
| `POST /v2/draft/supply/create/status` | `draft_supply_create_status_v2(operation_id)` | Polling для статуса финализации. |
| `POST /v1/draft/supply/create` | `draft_supply_create(...)` | Legacy v1 (старый async). |
| `POST /v1/draft/supply/create/info` | `draft_supply_create_info(operation_id)` | Legacy polling. |

### 7.5 Перенос таймслота / отмена

| Endpoint | Метод | Зачем |
|---|---|---|
| `POST /v1/supply-order/timeslot/update` | `supply_order_timeslot_update(...)` | Workaround при 429 на /timeslot/info: создать поставку и потом выставить слот. |
| `POST /v1/supply-order/cancel` | `supply_order_cancel(order_id)` | Отмена supply order (асинхронно). |
| `POST /v1/supply-order/cancel/status` | `supply_order_cancel_status(operation_id)` | Polling для отмены. |

**Раздел:** FBO Поставки (write).

⚠ Лимиты Ozon на этот блок:
- `/v1/draft/*/create` — 2 req/min, 50/час, 500/день (account-level)
- `/v2/draft/timeslot/info` — 2 req/sec **на всех продавцов** (глобальный)
- Драфт живёт 30 мин — переиспользуем 25 мин через `OzonDraftCache`

---

## 8. Статусы поставок (карточка заявки + утренняя рутина)

| Endpoint | Метод | Зачем |
|---|---|---|
| `POST /v3/supply-order/get` | `supply_order_get(order_ids)` | Детали по созданным supply order'ам (state, dropoff_warehouse, supplies). До 50 за раз. |

**Раздел:** FBO Поставки (read).

---

## Сводная таблица — какие разделы Seller API нужны

| Раздел в Ozon ЛК | Read | Write | Используется для |
|---|---|---|---|
| **Каталог товаров** | ✅ | — | Sync каталога |
| **Остатки на складах** | ✅ | — | Digest, карточка |
| **Заказы FBO** | ✅ | — | Digest (rate of sale) |
| **Возвраты** | ✅ | — | Digest, меню |
| **Склады** (FBO/FBS) | ✅ | — | Wizard, drop-off |
| **FBO Поставки** | ✅ | ✅ | Создание/отмена/перенос |

**Минимальный набор для бота:** Admin-ключ (всё read + write FBO). Без write-FBO юзер сможет видеть digest/возвраты/каталог но не создавать поставки в боте.

---

## Чего бот **НЕ делает** (ничего из этого не дёргается)

- ❌ FBS заказы (отгрузка через своего курьера, постинги, акт)
- ❌ Финансы (commissions, выплаты)
- ❌ Реклама / Promo
- ❌ Аналитика (отдельные analytics-эндпоинты)
- ❌ Чат с покупателями
- ❌ Жалобы / арбитраж
- ❌ Управление ценами / характеристиками товаров (только read каталога)

Если в будущем добавим эти фичи — нужно будет расширять права API-ключа.

---

## Если у тебя ограниченный API-ключ

Пришли скриншот или список разрешений (пунктов в Seller API → Настройки), я скажу что именно работать не будет.

Например, частые сценарии:
- **Read-only ключ** → digest, возвраты, остатки работают; создание поставок — нет.
- **Без раздела «Возвраты»** → digest покажет 0 возвратов / errors в errors-секции.
- **Без раздела «FBO Поставки»** → wizard создания поставки сломается на draft/create.

---

## Источник

- `ozon_api_docs.txt` (extracted из `Ozon Seller API v2.1.pdf` в корне репо).
- Все методы реализованы в `src/integrations/ozon_api.py`.
- Каждый метод снабжён docstring'ом с указанием endpoint'а, нюансов rate-limit и формата payload/response.
