# WORKLOG — Postavka Assistant Bot

Журнал изменений по сессиям. Свежие записи сверху. Дописывать новую запись после каждого осмысленного блока работы или при коммите. Не затирать старые.

## Формат

```
## YYYY-MM-DD HH:MM — Краткая суть
- что сделано (файл:строки если важно)
- почему (контекст, что не сработало)
- статус: работает / тестируется / blocked-by-X
```

---

## 2026-05-13 (10:30) — UX-блок: фильтр дат, русские статусы, ТЗ Отгрузка, возвраты Ozon+WB, скриншоты, pre-check SKU

Большой блок UX/функциональных правок после первой успешной поставки.

### Pre-check артикулов перед draft_create (Ozon)
- `src/bot/handlers/ozon_book.py:_validate_skus_in_current_account` — новая функция: тянет product_list+product_info_list текущего кабинета, возвращает (missing, sku→offer_id). Вызывается из `_create_drafts_and_fetch_scoring` и `_create_drafts` ДО draft_create.
- Если хоть один `ozon_sku` из заявки не найден в текущем кабинете — блокируем заявку с явным сообщением: «Стоп — артикулы не из текущего кабинета», список конкретных article+offer_id+sku, инструкция запустить `/sku_link_ozon`. Защита от багов после переезда между Ozon-аккаунтами.
- Почему: утром оказалось, что у нас в БД ozon_sku=1897274604 (для 3CHOC-35G) был от старого кабинета. На новом кабинете для этого offer_id другой sku. Это давало OUT_OF_ASSORTMENT на Ростове и не давало бы провернуть заявку.

### Чистка БД: пересинхрон SKU с актуальным Ozon-кабинетом
- Прогнал sync с текущим Ozon (client_id=2112912): 27 товаров, 19 совпало по offer_id, 3 по barcode, 3 значения `ozon_sku` обновлены, 2 SKU очищены (`ozon_sku=NULL`, нет в этом кабинете).
- TODO на потом: в БД есть мусорные SKU (`/startt`, `Валдбериз`, `коробки`, голые баркоды `2048580950269`, фрагменты `19-20`) — парсер xlsx иногда ловит лишние строки. Записано в memory как future_features.

### Парсер scoring: status=FAILED больше не маскируется под «дозревает»
- `_fetch_scoring_persistent`: на `status=FAILED` + `errors[].items_validation` сразу показываем реальную ошибку Ozon (например `OUT_OF_ASSORTMENT` для конкретного sku в конкретном кластере), без 4 ретраев по минуте.
- Почему: Ростов возвращал `{clusters:[], status:FAILED}` → парсер видел пустой `wh_list` → срабатывал «scoring дозревает». Прятали реальный отказ.

### Фильтр конкретных выбранных дат (Ozon)
- БД: добавлена колонка `shipment_requests.target_dates_json` (JSON, список ISO-строк) + миграция `1d4b0ab4021c_add_target_dates_json`.
- `cb_sp_confirm_dates` сохраняет список фактически отмеченных дат рядом с from/to (which всё ещё min/max для совместимости с WB-флоу).
- `_create_drafts_and_fetch_scoring` пробрасывает `ob_date_picks` в FSM state и далее в `_fetch_slots_for_drafts`/`_auto_poll_slots` как фильтр поверх слотов. Ozon-API принимает только `date_from..date_to` диапазоном — фильтруем у себя.
- Почему: пользователь тыкает 18, 20, 22 — Ozon возвращал слоты включая 19 и 21. Теперь видны только выбранные.

### Русские подписи статусов в UI
- `_state_label()` в shipment.py — словарь `draft → ✏ Черновик`, `planning → 📅 Запланировано`, `slot_searching → 🔍 Поиск слотов`, `supplies_created → ✅ Забронировано`. DB-значения остаются без изменений; перевод только в UI (список заявок + карточка).

### `ship_tz.py` приведён к эталону пользователя
- Полный rewrite под структуру `файлы для показа клоду/ТЗ Отгрузка шаблон.xlsx`:
  - Колонки динамические per (cluster, warehouse) — если есть `target_warehouse` после бронирования, шапка = имя склада, R2 = `booked_supply_id`, R3 = дата+таймслот формата «7 мая 18-19». Если не забронировано — шапка = кластер, R2/R3 пусто.
  - Озон: полные имена кластеров (не сокращаю), последняя колонка K = склад+таймслот per row.
  - Стили: bold + заливка `#FFF2CC` + thin border — как было у нас.
  - Ширины колонок взяты из эталона.
  - Лист «операции»: metadata заявки + блок «📋 Операции/заметки» из 10 merged-строк для ручного заполнения.

### Возвраты (новый модуль `src/bot/handlers/returns.py`)
- Главное меню → «📥 Возвраты» → подменю с двумя кнопками: Ozon и WB.
- **Ozon**: тянет `/v1/returns/list` (universal FBO+FBS) → фильтрует только actionable (статус «В пункте выдачи» / `ArrivedAtReturnPlace`) → показывает список с артикулами, складами, posting_numbers → пробует тянуть PDF через `/v1/return/giveout/get-pdf`. **Важно**: Ozon отдаёт base64-PDF в JSON-поле `pdf` (не `file_content` как в офиц. доке!) — поддерживаем оба ключа. Если PDF есть — присылаем документом, иначе показываем инструкцию открыть ЛК и нажать «Получить возвраты». Кнопка с длинным deeplink в правильный экран ЛК (status=30).
- **WB**: через `GET /api/v1/supplier/sales?dateFrom=30d&flag=0` Statistics API. Фильтр `saleID` начинающихся с `R` = возвраты-рефанды. Лимит API ~1 req/min — предупреждаем заранее. PDF через WB API недоступно — кнопка прямо в ЛК (`wildberries.ru/lk/myorders/delivery`).

### Скриншоты от пользователя
- `src/bot/handlers/upload.py:handle_photo` — обработчик `F.photo`. Юзер шлёт фото боту → сохраняется в `data/screenshots/{ts}_{uniq}.jpg` → бот отвечает путём → Claude читает через Read tool (поддерживает PNG/JPG).
- `data/screenshots/` добавлен в `.gitignore`.

### Layout эталона — откатил частичную правку
- Был промежуточный эксперимент с R2/R3 на озон-листе (supply_id+slot сверху). Пользователь сказал «забудь» — откатил, оставил только в `вб`-листе как в эталоне.

### .gitignore
- `data/screenshots/` — игнорируем.
- `файлы для показа клоду/` — игнорируем (там docx-стратегия, эталоны xlsx, PDF Ozon-доки, переписки). Раньше эти файлы были в корне, юзер их переместил в эту папку.

### Статус
Бот рестартован финально 10:29 после серии правок дня. Все правки прошли smoke-тест в чате с пользователем.

---

## 2026-05-13 (09:22) — 🎉 ПЕРВАЯ УСПЕШНАЯ Ozon-поставка через бота

- **order_id=104181472, status=SUCCESS** на ПУШКИНО_2_РФЦ (id=23902289166000), кластер «Москва, МО и Дальние регионы», слот 2026-05-17 15:00–16:00.
- Полный успешный trace:
  1. `/v1/draft/direct/create` → 200, draft_id=108573581
  2. `/v2/draft/create/info` → 200, 2 склада available (ПУШКИНО rank=1 score=0.996, ДОМОДЕДОВО rank=2 score=0.72)
  3. `/v2/draft/timeslot/info` → 168 слотов
  4. `/v2/draft/supply/create` → 200 `{"error_reasons":[], "draft_id":108573581}`
  5. `/v2/draft/supply/create/status` → 200 `{"order_id":104181472, "status":"SUCCESS"}` (с первой попытки polling, ~1 сек)
- Финальный фикс перед успехом: убран `Z` из timeslot.from/to_in_timezone в payload v2 (v2 ругается `"extra text: \"Z\""`). Разница с v1: v1 требовал Z, v2 запрещает.
- Статус: **MVP Ozon-флоу замкнут end-to-end.** Можно тестировать на повседневных заявках.

---

## 2026-05-13 (09:17) — Финализация supply переведена на /v2 + удалён дубликат бронирования

- `src/integrations/ozon_api.py`:
  - Добавлен `draft_supply_create_v2(draft_id, cluster_id, warehouse_id, timeslot_from/to, supply_type)` → POST `/v2/draft/supply/create`. Payload v2: `draft_id` + `selected_cluster_warehouses[{macrolocal_cluster_id, storage_warehouse_id}]` + `timeslot` + `supply_type` (enum). Возвращает список error_reasons (пустой = ack ok).
  - Добавлен `draft_supply_create_status_v2(draft_id)` → POST `/v2/draft/supply/create/status` для polling. Ответ: `{error_reasons, order_id, status: UNSPECIFIED|SUCCESS|IN_PROGRESS|FAILED}`.
  - `/v2/draft/supply/create` и `/v2/.../status` добавлены в `_GLOBAL_LIMIT_PATHS`.
  - Для `/v2/draft/supply/create` явно `retries_on_429=1` (не 5). Финальный endpoint самый чувствительный — лишние ретраи могут спровоцировать настоящий account-ban. Лучше пользователь сам жмёт «🔁».
- `src/bot/handlers/ozon_book.py`:
  - `supply_type` добавлен в `drafts_made` dict (в `_create_drafts`, рядом с `cluster_id`).
  - `cluster_id` + `supply_type` пробрасываются в slot-dict в обоих местах: FSM `slot_{n}` (в `_fetch_slots_for_drafts`) и `_FOUND_SLOTS` (в `_auto_poll_slots`).
  - `_do_book_slot` переписан под v2: один блок (create_v2 → polling status_v2 → запись в БД).
  - **Удалён дубликат блока бронирования** (старые строки 1433-1493). Latent bug: при успехе первого create скрипт делал ВТОРОЙ полный create + polling. На 429 не срабатывал (early return по исключению), но на success — выстрелил бы.
- Почему: ChatGPT прислал подсказку, я проверил доки v2.1 (`ozon_api_docs.txt:14832`): с 16.03.2026 v1 supply/create задеприкейчен, v2 ждёт другой payload. ChatGPT же заметил дубликат блока в `_do_book_slot`.
- Статус: бот рестартован 09:17, готов к тесту.

---

## 2026-05-13 (09:00) — Фикс парсера scoring v2: state enum + дубликаты wh_list

- `src/bot/handlers/ozon_book.py:_fetch_scoring_persistent`:
  - `available_states` расширен: `{"FULL_AVAILABLE", "PARTIAL_AVAILABLE", "AVAILABLE", "SUCCESS"}`. v2 отдаёт `FULL_AVAILABLE`/`PARTIAL_AVAILABLE` — старый парсер их не знал, считал склады недоступными.
  - `pending` теперь только `state == "UNSPECIFIED"`. Раньше: `invalid_reason == "UNSPECIFIED"` тоже триггерил pending — но в v2 это значит «нет причины невалидности» (склад **валидный**), а не «считается».
  - `wh_list = []` перенесён внутрь `for attempt` — раньше объявлен снаружи, дубликаты складов накапливались между ретраями (`total=16 → 32 → 48 → 64` в логе).
- Почему: лог показал сырой v2-ответ — `availability_status: {state: FULL_AVAILABLE, invalid_reason: UNSPECIFIED}` для ПУШКИНО (rank=1, score=0.996) и ДОМОДЕДОВО (rank=2, score=0.67). Парсер считал их pending+unavailable → бот крутил 4×60 сек впустую и завершался с «scoring не посчитался».
- Статус: бот рестартован 09:00, готов к тесту.

---

## 2026-05-13 (08:15) — Scoring info переведён на /v2/draft/create/info

- `src/integrations/ozon_api.py`: `draft_create_info(draft_id=...)` теперь ходит в `POST /v2/draft/create/info`; старый `POST /v1/draft/create/info` оставлен только для legacy `operation_id`. `/v2/draft/create/info` добавлен в `_GLOBAL_LIMIT_PATHS`.
- `src/bot/handlers/ozon_book.py`: парсер scoring поддерживает v2-ответ: `storage_warehouse` + `availability_status`, а также статус `IN_PROGRESS`.
- Почему: по локальной документации Ozon после `/v1/draft/direct/create` / `/v1/draft/crossdock/create` возвращается `draft_id`, и проверять его надо через `/v2/draft/create/info`. Мы отправляли `draft_id` в старый `/v1/draft/create/info`, который по docs ждёт `operation_id`; это могло давать стабильный 429 на двух аккаунтах и разных IP.
- Проверка: `python -m py_compile src\integrations\ozon_api.py src\bot\handlers\ozon_book.py` — OK. Нужен рестарт бота и ручной тест `/ozon_book`.

---

## 2026-05-13 (08:05) — SOCKS5-прокси через prox6.net (новый исходящий IP)

- `.env`: `OZON_PROXY_URL=socks5://duGqx8:bLfWcs@91.198.215.66:8000` (раскомментирован).
- `src/integrations/ozon_api.py`: в `_request` добавлена ветка под SOCKS-схемы (`socks4://`, `socks5://`, `socks5h://`). Если URL начинается с socks — используем `httpx_socks.AsyncProxyTransport.from_url(..., rdns=True)` вместо `httpx.AsyncClient(proxy=...)`. Это критично: без `rdns=True` httpx сам резолвит DNS и SOCKS-прокси отдаёт `code 2` (ruleset not allowed). С remote DNS — прокси резолвит сам.
- Поставлен пакет `httpx-socks` (через pip).
- Гипотеза про IP-троттлинг подтверждена косвенно: вчерашние ливни 429 с домашнего IP `213.165.43.183` могли занизить нам квоту, прокси даёт чистый IP `91.198.215.66` (RU).
- Smoke-тест через OzonClient с прокси: stocks_fbo=5, cluster_list=22, оба 200 OK.
- Снова сбросил `ozon_cooldown.json` — на свежем IP старый cooldown не нужен.
- Гайды/нюансы по этому прокси сохранены в memory.
- Статус: бот рестартован 08:05.

---

## 2026-05-13 (07:55) — Scoring «спокойный режим» + SKU 3CHOC-35G

- `_fetch_scoring_persistent` в `ozon_book.py`: было 6 × 30 сек = 3 мин (с внутренними 4 ретраями _request), стало **4 × (60+jitter 0-30) сек ≈ 4-6 мин**. Один запрос за итерацию.
- `OzonClient.draft_create_info` default `retries_on_429`: 4 → **0**. Внутренние ретраи убраны — они быстро забивали ту же per-second квоту и тратили слоты впустую.
- Идея от ChatGPT (через юзера) — резонная: при глобальной перегрузке частые попытки только продлевают штрафной режим. Реже + jitter = больше шансов попасть в свободное окно, не усугубляя.
- БД: добавлен SKU `3CHOC-35G` (id=44, склонирован с базового `3CHOC` id=10, name + ' (35г)', ozon_offer_id='3CHOC-35G'). В файле опись на новом аккаунте этот артикул писался с граммовкой, в локальном каталоге был без неё → не матчилось.
- Статус: бот рестартован 07:55.

---

## 2026-05-13 (07:35) — Откат cooldown с /v1/draft/create/info + нормальные ретраи

- `src/integrations/ozon_api.py`: `/v1/draft/create/info` **убран** из `anti_abuse_paths`. Это read-only scoring, 429 здесь = глобальный per-second лимит Ozon (как `/timeslot/info`), а не account-бан. 15-мин cooldown с прошлой правки (12.05 15:30) блокировал весь booking-флоу с первого же 429 на свежем аккаунте.
- `draft_create_info()`: теперь принимает `retries_on_429` (default 4) и зовёт `_request` напрямую с пробросом ретраев. Раньше шёл через `_post()` → `retries_on_429=0` для global → одна попытка и фатал.
- `_request()` backoff list для is_global: было `[]` (пустой → IndexError если Retry-After не пришёл), стало `[2, 3, 4, 5, 5, 5]`. Это же и для timeslot/info теперь корректно — раньше работало только благодаря Retry-After header'у от Ozon.
- `data/cache/ozon_cooldown.json` снесён — на старте новый аккаунт чистый.
- `.env`: переключён на запасной аккаунт client_id=2154472 (старый закомментирован).
- Почему: на свежем client_id'е (2154472) первый же запрос create/info → 429 → бот валился в 15-мин cooldown. Пользователь резонно: «на новом аккаунте бана быть не может, это в коде». Прошлая правка (15:30 12.05) была оверкилл — для тяжёлого account-бана на старом аккаунте имела смысл, для нормальной работы блокирует флоу.
- `/v1/draft/supply/create` и `/v1/draft/supply/create/info` оставлены в `anti_abuse_paths` — это действительно account-level (Ozon SS подтверждали).
- Статус: бот рестартован 07:35, готов к тесту на новом client_id'е.

---

## 2026-05-12 (15:30) — Anti-abuse правки: убрать blind-pick, cooldown на create/info, гасить auto-poll на 404

- `src/integrations/ozon_api.py`: `/v1/draft/create/info` добавлен в `anti_abuse_paths` → после 429 ставится 15-мин cooldown (для supply/create — 30 мин). Раньше cooldown на create/info вообще не ставился, и каждый поток мог снова дёргать его → продлевать anti-abuse бан.
- `src/bot/handlers/ozon_book.py:_fetch_scoring_persistent`: окно увеличено с 2×5с=10с до 6×30с=3мин. На пустой scoring **больше не делается blind-pick из cluster_list** (это создавало 404 на timeslot/info и было главной причиной CROSSDOCK-проблемы). Cooldown-ошибка от клиента → сразу бросаем без ретраев.
- `src/bot/handlers/ozon_book.py:_auto_poll_slots`: на `404 scoring` фоновая задача гасится с сообщением пользователю «связка draft+склад невалидна, создай draft заново». Раньше auto-poll бил 404 каждые 60 сек 25 мин подряд → продлевал бан.
- Почему: ChatGPT и я независимо пришли к одному корню — blind fallback на wh из `cluster_list` подсовывал склад, которого нет в scoring текущего draft → 404 «warehouse scoring result not found». Раньше скрыто было лимитом ретраев scoring (10 сек), теперь же при 429-стене scoring не успевал и fallback был основной путь.
- Статус: бот пока **не перезапускал** — Ozon аккаунт сейчас в anti-abuse cooldown (бьёт supply/create + create/info), любой свежий хит может продлить бан. Перезапустить после паузы ≥30 мин с момента последнего 429 (примерно ~15:50 по UTC).

---

## 2026-05-12 (15:16) — Фикс CROSSDOCK timeslot/info #2: убрать wh для не-DIRECT

- `src/integrations/ozon_api.py`: `storage_warehouse_id` теперь передаётся **только для DIRECT**. Для CROSSDOCK/MULTI_CLUSTER в `selected_cluster_warehouses` — только `macrolocal_cluster_id`.
- memory `reference_ozon_fbo_api.md`: уточнено правило per-supply_type, добавлен симптом 400 «not allowed parameter warehouse_id».
- Почему: после первого фикса (15:14) Ozon вернул 400 «Request validation error: not allowed parameter warehouse_id for specified supply type». Для CROSSDOCK хаб уже зашит в draft на этапе draft/crossdock/create через `delivery_info.drop_off_warehouse` — timeslot тянется именно для него, передавать wh в timeslot/info нельзя.
- Статус: перезапущен в 15:16:53, ждём CROSSDOCK-теста.

---

## 2026-05-12 (15:14) — Фикс CROSSDOCK timeslot/info: единое поле storage_warehouse_id

- `src/integrations/ozon_api.py`: убрал ветвление по `supply_type` для имени wh-поля. Теперь всегда `selected_cluster_warehouses[].storage_warehouse_id`, как требует PDF (changelog ozon_api_docs.txt:36045).
- memory `reference_ozon_fbo_api.md`: исправлена эмпирика — `drop_off_warehouse_id` есть только в RESPONSE, в REQUEST его нет. Передача этого поля → 404 со специфичным пустым wh в тексте ошибки.
- Почему: первый фикс типов не помог CROSSDOCK — каждый запрос отдавал 404 «scoring result not found  in cluster variant» с двойным пробелом перед «in». Двойной пробел = wh-плейсхолдер в шаблоне ошибки пустой = Ozon не распарсил wh из payload. Старая memory предлагала `drop_off_warehouse_id` для CROSSDOCK — это и оказалось ошибкой выводки в той сессии.
- Статус: бот перезапущен (15:14), готов к тесту CROSSDOCK.

---

## 2026-05-12 — Фикс типов FBO Draft API: int → string-enum

- `src/integrations/ozon_api.py`: `supply_type` `1/2/3` → `"CROSSDOCK"/"DIRECT"/"MULTI_CLUSTER"`, `deletion_sku_mode` `1` → `"PARTIAL"`, `delivery_info.type` `1` → `"DROPOFF"`, `warehouse_type` `1` → `"DELIVERY_POINT"`. Добавлен helper `_supply_type_str()` — handlers продолжают передавать int, трансляция внутри клиента.
- `.gitignore`: исключены `Документация Ozon Seller API.pdf` (14 МБ) и `ozon_api_docs.txt`.
- memory: переписан `reference_ozon_fbo_api.md` под актуальный PDF v2.1; добавлен `reference_ozon_api_pdf.md`.
- Почему: пользователь прислал свежую офиц. документацию Ozon Seller API v2.1 — после 16.03.2026 типы payload поменялись с int-кодов на enum-строки. На CROSSDOCK падало `404 /v2/draft/timeslot/info: warehouse scoring result not found` потому что Ozon молча трактовал старый int как UNSPECIFIED и не находил scoring.
- Статус: **готов к тесту на боте** (DIRECT и CROSSDOCK).

---

## 2026-05-12 14:20 — Итог дневной сессии Ozon API

**За день (07:30-14:20):**

### Реализовано и работает ✅
- Ozon Draft API на новые endpoints (03.2026): `/v1/draft/direct/create` sync-режим с draft_id
- v2 timeslot/info: поднята правильная схема payload (supply_type, macrolocal_cluster_id, storage_warehouse_id singular) + parsing v2 response под `result.drop_off_warehouse_timeslots: {days:[...]}`
- Scoring fetch через `/v1/draft/create/info` — picker показывает только available склады с rank/score
- Auto-walk: бот сам перебирает scored склады до первого 200 со слотами
- Auto-poll background: каждые 60 сек на 25 мин при 429
- Курированный приоритет складов (ДОМОДЕДОВО→ХОРУГВИНО→ПУШКИНО для Москвы)
- UX: главное меню кнопками, edit_text вместо новых сообщений, sticky-чекбоксы в date picker
- BotCommand в панели Telegram (/start, /ship, /help, /cancel)
- Логирование: logs/bot.log с ротацией 5MB×5
- WORKLOG.md + memory-файлы дисциплинированно ведутся

### Заблокировано Ozon-баном 🚫
- `/v1/draft/supply/create` — Ozon забанил аккаунт после retry-штормов. К 14:15 ещё не отпустил (>1ч без запросов). Скорее всего unblock через 24h или next midnight МСК.
- timeslot/info фактически работает (176 слотов на Пушкино пришли в 13:00) — только финальное бронирование зажато.

### Anti-abuse фикс ✅
- Cooldown 30 мин для supply/create на нашей стороне — не даём пользователю ретыкать и продлевать бан.

### Извлечённые уроки → memory
- `reference_ozon_fbo_api.md` — полная карта Ozon FBO API: endpoints, payload, response, лимиты, anti-abuse. Кровью добыто.
- `reference_wb_api_limits.md` — поправил себя: для FBW write-API нет, только read.

### Открытые задачи
- CROSSDOCK режим в wizard (рядом с DIRECT) — может Ozon не забанил `/v1/draft/crossdock/create`. Тестируем сейчас.
- Привязка 7 SKU (3CHOC, KINDER-JOY-*) к Ozon offer_id — без этого draft создаётся с 1 SKU вместо 8.
- WB-сторона мониторинга существующих поставок (low-prio).

### Гипотеза почему nepsell работает
- Скорее всего commercial partner integration с Ozon → высокие квоты на их client_id, не наш. Их public docs (через user) описывают forecast/planning, не slot-booking — booking в их UI идёт через скрытый API. Сравнивать с прямой Seller API нельзя.

---

## 2026-05-12 12:30 — 🎉🎉 Парсинг v2 ответа: слоты были всегда, просто структура другая

**Контекст.** Юзер прислал сырой dump ответа `/v2/draft/timeslot/info` (получили после моей правки логирования) — оказалось слоты есть, мы их просто не парсили правильно.

**v1 структура (legacy):**
```json
{
  "drop_off_warehouse_timeslots": [
    {"warehouse_id": ..., "warehouse_name": "...", "days": [...]}
  ]
}
```

**v2 структура (наш случай — 1 склад через selected_cluster_warehouses):**
```json
{
  "result": {
    "drop_off_warehouse_timeslots": {     ← объект, не массив!
      "days": [
        {"date_in_timezone": "2026-05-15", "timeslots": [
          {"from_in_timezone": "...", "to_in_timezone": "..."}
        ]}
      ]
    }
  }
}
```

Два отличия:
1. **Обёртка `result.`** на верхнем уровне (мы читали с корня)
2. **Объект вместо массива** для timeslots — потому что мы запросили 1 конкретный склад

**Сделано:**
- `src/bot/handlers/ozon_book.py`: helper `_parse_v2_timeslots(ts, fallback_wh_id, fallback_wh_name)` — корректно парсит **обе** структуры (v1 массив и v2 объект под result).
- Парсер вызывается во всех трёх местах: `_fetch_slots_for_drafts`, `_auto_poll_slots` (background), `cb_ob_autowalk` (`_try_wh`).

**Статус:** перезапущено 12:30. Теперь юзер должен реально увидеть СЛОТЫ когда тапает конкретный склад. Auto-walk тоже должен находить.

---

## 2026-05-12 11:44 — 🎉 v2 timeslot/info заработал + UX-правки

**Контекст.** Через серию экспериментов и чтения исходников Go-клиента `bryxosmmm/ozon-api-client` собрана правильная схема payload для `/v2/draft/timeslot/info`:

```json
{
  "draft_id": 108XXXXXXX,
  "date_from": "2026-05-18",   ← YYYY-MM-DD, не ISO с T...Z
  "date_to": "2026-05-23",
  "supply_type": 2,            ← 1=CROSSDOCK, 2=DIRECT (выявлено экспериментом)
  "selected_cluster_warehouses": [{
    "macrolocal_cluster_id": 4039,
    "storage_warehouse_id": 1020001853757000  ← SINGULAR, не array
  }]
}
```

**Эволюция ошибок Ozon до правильной схемы:**
1. `value does not match regex YYYY-MM-DD` → срезали ISO время
2. `selected_cluster_warehouses must contain 1-20 items` → добавили поле
3. `MacroLocalClusterId required` → переименовали cluster_id → macrolocal_cluster_id
4. `SupplyType must not be in list [0]` → добавили supply_type=1 (DIRECT, как думали)
5. `Requested wrong delivery flow. Draft is Direct` → перепутали enum, на самом деле 2=DIRECT
6. `when supply type is DIRECT, invalid storage warehouse_id` → переименовали storage_warehouse_ids → storage_warehouse_id (singular)
7. **🎉 200 OK** — Ozon ответил «слотов нет на эти даты»

**Сделано:**
- `src/integrations/ozon_api.py`: финальная схема v2 payload — поле `storage_warehouse_id` (singular), supply_type=2 для DIRECT.
- `src/bot/handlers/ozon_book.py`: убрана опция «🎲 Любой склад» (бот выбирал автоматически — теперь только «🎲 Без фильтра» или конкретный склад).
- `src/bot/handlers/shipment.py`: при «🛠 Изменить даты» в карточке заявки — галочки прошлого выбора **сохраняются** в календаре. Подсказка «Ранее выбрано: N даты».
- `_start_plan_wizard` читает `target_date_from/to` и предзаполняет offsets.

**Статус:** перезапущено 11:44. Ozon API работает! Если слотов нет — расширить даты или попробовать другой склад.

---

## 2026-05-12 11:11 — Переключение на /v2/draft/timeslot/info (тест)

**Контекст.** Пользователь спросил почему nepsell работает а у нас нет. Гипотеза: nepsell использует v2 endpoint и/или имеет partner-API. Пробуем v2.

**Сделано:**
- `src/integrations/ozon_api.py`: добавил константу `OzonClient.TIMESLOT_INFO_PATH = "/v2/draft/timeslot/info"` (раньше hardcoded `/v1/draft/timeslot/info`).
- Метод `draft_timeslot_info` теперь использует эту константу — можно быстро переключаться между v1 и v2.
- `_GLOBAL_LIMIT_PATHS` уже содержал и v1 и v2 — retry-логика работает для обоих.

**Если v2 не помогает:**
- nepsell.ru/docs — SPA, контент рендерится JS, через curl не достать. WebFetch тоже пустой.
- Скорее всего nepsell использует Ozon Partner API (commercial integration) и/или константно поллит в фоне для всех клиентов с собственным пулом IP. Точно установить без их docs нельзя.

**Статус:** перезапущено 11:11, тестируем v2.

---

## 2026-05-12 10:57 — Выбор drop-off склада в Ozon-wizard (стратегия другого бота)

**Контекст.** Пользователь поделился что в их текущем сервисе выбирается склад (либо «любой» — бот сам, либо конкретный), draft создаётся tied к этому складу, и поллинг идёт по нему. У них Pushkino часто срабатывает, но возможен любой из FF. Реализую похожую UX.

**Сделано:**
- `src/bot/handlers/ozon_book.py`:
  - Новый state `OzonBook.pick_warehouse`.
  - Курированный приоритет складов по кластеру: `_WAREHOUSE_PRIORITY = {"москва": ["ДОМОДЕДОВО", "ХОРУГВИНО", "ПУШКИНО", "СОФЬИНО", "ЖУКОВСКИЙ", "ВАТУТИНКИ"]}`. Домодедово — первое, потому что ЛЕБЕР там же.
  - Чёрный список слов: НЕГАБАРИТ, КГТ, ШИНЫ, АПТЕКА, ВЕТАПТЕКА, ФОТОСТУДИЯ, ПАЛЛЕТНЫЙ, КРОССДОКИНГ — отфильтровываются (специализированные склады).
  - `_get_cluster_ff_warehouses(cluster_name)` — читает кэш `ozon_clusters`, возвращает только FULL_FILLMENT-склады нужного кластера, отсортированные по приоритету.
  - `_ask_warehouse_for_cluster` — показывает топ-6 кнопок + «🎲 Любой» (бот: первый по приоритету) + «📋 Показать все» (если складов больше).
  - Кэш итерации: `ob_wh_choices: Dict[cluster, wh_id]`, `ob_cluster_idx` — поддерживает мульти-кластер заявки.
  - Callbacks `obwh:any:<idx>`, `obwh:<idx>:<wh_id>`, `obwhall:<idx>`, `obwhback:<idx>`.
  - `_create_drafts` теперь читает `ob_wh_choices` и передаёт `drop_off_point_warehouse_id` в `draft/direct/create`.
  - `_fetch_slots_for_drafts` и `_auto_poll_slots` передают `warehouse_ids=[wh_id]` в `draft_timeslot_info` — фильтр по конкретному складу.

**UX-флоу после правки:**
1. Тап «🚀 Создать поставку Ozon» в карточке заявки
2. Бот показывает summary + предлагает выбор склада для каждого Ozon-кластера
3. Тап «🎲 Любой» / «🎯 Конкретный»
4. Draft создаётся для выбранного склада
5. Auto-poll получает слоты только этого склада

**Преимущества:**
- UX яснее: ясно куда грузим
- Можно перепробовать склады (если один не отдаёт слотов, заявка остаётся — переоткроешь wizard с другим)
- Меньше «шума» в ответе timeslot/info — приходит только нужный склад

**Не лечит:** глобальный 2 req/sec лимит на endpoint всё ещё бьёт. Но auto-poll каждые 60 сек продолжает работать как раньше.

**Статус:** перезапущено 10:57.

---

## 2026-05-12 10:38 — Авто-поиск слотов в фоне каждые 60 сек

**Контекст.** Пользователь сказал что в их текущем бот-сервисе используется похожая логика (черновик → потом слоты), но **поиск идёт каждую минуту** в фоне. Это правильная стратегия против глобального 2 req/sec — наш одноразовый retry на 20 сек / 80 сек шансов мало даёт.

**Сделано:**
- `src/bot/handlers/ozon_book.py`:
  - Модульный кэш `_AUTO_POLL_TASKS: Dict[rid, asyncio.Task]` — фоновые задачи.
  - Модульный кэш `_FOUND_SLOTS: Dict[token, slot_dict]` — найденные слоты, чтобы callback `obfslot:<token>` работал без зависимости от FSM (пользователь мог выйти из мастера к моменту когда слот пришёл).
  - Новая функция `_auto_poll_slots(bot, chat_id, rid, drafts, date_from, date_to)`:
    - Цикл с интервалом 60 сек, до 25 мин (draft живёт 30 мин).
    - Каждая итерация: вызывает `draft_timeslot_info` для всех drafts.
    - При успехе → постит слоты пользователю, завершается.
    - Если API ответил но слотов реально нет → сообщает «расширь даты», завершается.
    - Каждые 5 минут — обновляет статус-сообщение (edit_text) «попытка N, лимит держит».
    - На таймаут 25 мин → сообщает что drafts протухнут, нужно пересоздать.
  - `_post_found_slots`: рендерит найденные слоты с inline-кнопками `obfslot:<rid>_<i>`.
  - Новый callback `cb_ob_found_slot_pick` (`obfslot:` префикс) — бронирование выбранного слота (clone логики `cb_ob_slot_pick` без зависимости от FSM).
  - Новый callback `cb_ob_cancel_poll` (`obcancelpoll:` префикс) — кнопка «✖ Остановить авто-поиск».
  - В `_fetch_slots_for_drafts` при 429-сценарии: запускает auto-poll и сообщает пользователю «авто-поиск каждые 60 сек запущен в фоне».
- `src/integrations/ozon_api.py`:
  - `draft_timeslot_info` принимает `retries_on_429` (для авто-пуллинга кладём 2-3, чтобы не блокировать цикл надолго).

**Логика:** initial-окно 20 сек (5 ретраев) даёт быстрый результат если лимит свободен. Если упёрся → background poll каждые 60 сек в течение 25 мин. Пользователь может пойти заниматься своими делами — придёт нотификация когда слоты найдутся.

**Статус:** перезапущено 10:38. Готово к тесту: пользователь жмёт «🚀 Создать поставку Ozon» → если 429 → бот говорит «авто-поиск каждые 60 сек запущен», пишет позже когда найдёт.

---

## 2026-05-12 10:20 — Перенастройка retry: короткое окно + ясный текст ошибки

**Контекст.** Тест в 10:15-10:17 показал: 21 попытка timeslot/info за ~80 сек — всё в 429. Глобальный лимит Ozon реально перегружен прямо сейчас. Пользователь спросил «слотов нет почему — реально нет или ошибка?» — путаница из-за формулировки бота «Подходящих слотов не нашлось».

**Сделано:**
- `src/integrations/ozon_api.py`: retry-budget global-limit paths 20 → 5 попыток (`[2,3,4,5,6]` сек, ~20 сек суммарно). Сидеть 80 сек впустую — хуже чем быстро узнать результат и тыкнуть retry-кнопку в своём темпе.
- `src/bot/handlers/ozon_book.py`:
  - Сообщение при 429: чёткое разделение **«🚫 Ozon заблокировал запрос»** (наш случай — 429) vs **«🔴 Реально нет слотов на даты»** (если 200 OK но пусто).
  - Объяснение пользователю что лимит общий для всех продавцов (не наш аккаунт), drafts уже созданы, можно ретраить.
  - Пауза перед первым timeslot/info: 5 → 3 сек.
  - Текст «📅 Таймслоты…» теперь говорит «до 20 сек» а не «до 30/80».

**Состояние:** Перезапущено 10:20. Глобальный rate-limit Ozon продолжает упираться — это сейчас НЕ наш баг, а перегрузка Ozon-инфры (см. тикет Ozon SS в переписке: «подумают над увеличением лимита»). Ничего больше из нашего кода с этим не сделать. Альтернативы: ждать, использовать /ship_tz для ручного создания через ФФ, переключиться на WB-направления / SKU линкинг.

---

## 2026-05-12 09:48 — Усиление retry-стратегии + кнопка «🔁 Повторить»

**Контекст.** После предыдущей итерации (09:24) timeslot/info всё равно упёрся в 429 за 8 ретраев (25 сек окно). При этом draft в Ozon ЛК создан — глупо его пересоздавать (тратим 2/мин на draft_create).

**Сделано:**
- `src/integrations/ozon_api.py`: retry-budget для global-limit paths поднят с 8 → 20 попыток, общее окно ~80 сек (паузы `[2,2,3,3,3,4,4,4,4,5×11]`).
- `src/bot/handlers/ozon_book.py`:
  - Извлечена функция `_fetch_slots_for_drafts(msg, state)` — изолированная попытка достать слоты для уже созданных drafts.
  - Cache в FSM state: `ob_drafts`, `ob_date_from_iso`, `ob_date_to_iso`.
  - Если ни одного слота не нашлось — state НЕ чистим, показываем кнопки «🔁 Повторить поиск слотов», «🌐 Ozon ЛК → Черновики», «◀ К карточке».
  - Новый callback `cb_ob_retry` дёргает `_fetch_slots_for_drafts` без пересоздания draft.

**Открытый вопрос:** В переписке 09:45 пользователь усомнился что черновик реально создаётся в ЛК (sync-ответ `draft_id` от нового endpoint vs реальный draft в LK). Ждём подтверждения от пользователя проверкой в `seller.ozon.ru/app/supply-orders/drafts`. Если draft не виден — задача глубже: новый sync API возможно возвращает draft_id «на этапе калькуляции», нужен дополнительный финализирующий вызов.

**Не сделано (отложено):** Персистить drafts в БД (выживут рестарт бота). Сейчас живут только в FSM state — пока пользователь не использует /cancel или не происходит рестарт бота, drafts доступны для retry в течение 30 мин жизни draft.

**Статус:** перезапущено 09:48, готово к тесту: пользователь нажимает «🚀 Создать поставку Ozon» → если timeslot/info упрётся снова, тапает «🔁 Повторить».

---

## 2026-05-12 09:39 — UX-рефакторинг: меньше команд, больше edit_text

**Контекст.** Пользователь сформулировал правило: всё через инлайн-кнопки, никаких слэш-команд кроме `/start`, `/help`, `/cancel`. Сообщения редактируются, а не плодятся. (Записано в memory: `feedback_inline_buttons_only.md`.)

**Сделано:**

`src/bot/handlers/common.py` — переписан с акцентом на render-функции и подменю:
- Главное меню: 📋 Мои заявки / 🔗 Привязать каталог / 🛠 Диагностика / 📚 Справка.
- Подменю «🛠 Диагностика» — кнопки на `api_check`, `api_warmup`, `wb_coefs`, `ozon_warehouses` (раньше доступны только командами).
- `cb_menu_home`, `cb_menu_help`, `cb_menu_sku_link`, `cb_menu_diag` — теперь все через `safe_edit_or_answer` (редактируют, не плодят).
- `cb_cancel` после отмены возвращает в главное меню.
- Тексты вынесены в константы `_MAIN_TEXT`, `_HELP_TEXT`.
- Кнопки «◀ Назад» / «🏠 Главное меню» во всех подменю.

`src/bot/handlers/shipment.py`:
- `cb_ship_open`, `cb_ship_del`, `cb_ship_more` — все через `safe_edit_or_answer`.
- `cb_ship_del` после удаления возвращает к списку заявок (не к голому "удалено").
- `cb_ship_more` — отображает приглашение «кинь файл» как edit + кнопка «◀ К списку».
- `cb_ship_plan` теперь редактирует карточку в календарь, а не открывает новое сообщение.
- `_show_confirm` (подтверждение плана) использует edit.
- `cb_sp_confirm_dates` — убрана промежуточная плашка «Целевые даты», сразу edit в confirm.
- `cb_skip_direction` — после скипа возвращает в карточку заявки (edit).
- `_start_plan_wizard` приобрёл параметр `edit=True/False`.

**Не тронуто (намеренно, на будущее):**
- `_run_hunt` (разведка слотов): много прогресс-сообщений во время API-вызовов — могут идти отдельными сообщениями, это OK. Сводный rewrite в single status message — следующая итерация.
- `_create_drafts` / `cb_ob_slot_pick` в ozon_book.py — то же самое, прогресс-сообщения по этапам API.
- Старые `/sku_list`, `/sku_add`, `/sku_kit_add` (catalog.py) — пока остаются командами, кнопочного входа в «📦 Каталог» ещё нет. Если будут нужны часто — добавлю отдельным меню.

**Статус:** перезапущено в 09:39, базовая навигация работает. Требует ручного теста: `/start` → клик через всё меню.

---

## 2026-05-12 09:24 — Обход глобального rate-limit Ozon timeslot/info + retry-стратегия

**Контекст.** Создание поставки Ozon через `/ozon_book` упиралось в 429 на `/v1/draft/timeslot/info`. Из тикета Ozon SS известно: это **глобальный** лимит 2 req/sec на ВСЕХ продавцов, не account-level. 5-минутный cooldown в этом случае бесполезен — лимит общий с другими, надо просто упорно ретраить.

**Сделано:**
- `src/integrations/ozon_api.py`:
  - Выделил `_GLOBAL_LIMIT_PATHS` = {`/v1/draft/timeslot/info`, `/v2/draft/timeslot/info`, `/v1/supply-order/timeslot/update`}.
  - В `_request`: для global-limit paths — 8 ретраев с короткими паузами `[2, 2, 2, 3, 3, 4, 5, 6]` сек (~25 сек окно), без 5-мин cooldown.
  - Для account-level paths (draft/*/create) — поведение прежнее (1 ретрай, потом 5-мин cooldown).
  - Добавил метод `supply_order_timeslot_update` — fallback по совету Ozon SS (создать поставку без слота → потом выставить через этот endpoint). Пока не используется.
- `src/bot/handlers/ozon_book.py`:
  - Пауза перед `timeslot/info` поднята с 1.5 → 5 сек (подальше от draft_create, ближе к глобальному окну).
  - Сообщение пользователю: предупреждение «может занять до 30 сек».
  - При ошибке — подсказка «черновик в ЛК уже есть, можешь руками дойти».

**Статус:** перезапущено, требует ручного теста через `/ozon_book` на заявке #6 (там уже создан draft_id=108279408 в 09:00).

**Если не пробьётся:** Реализовать fallback через `supply-order/timeslot/update` — но это требует чтобы supply мог создаваться без timeslot (схема `/v1/draft/supply/create` пока неизвестна, надо проверить).

---

## 2026-05-12 07:30 — 09:00 — Большой UX-рефактор + Ozon Draft API под новые endpoints (03.2026)

Сессия с длинной перепиской (см. `чо делали.txt` если ещё лежит в корне). Основные блоки:

### Ozon Draft API — переход на новые endpoints
- `/v1/draft/create` отключён 16.03.2026. Используем:
  - `/v1/draft/direct/create` — прямая
  - `/v1/draft/crossdock/create` — кроссдок
  - `/v1/draft/multi-cluster/create` — мульти-кластер
- Лимиты: 2/мин, 50/час, 500/день, draft живёт 30 мин.
- Структура payload новая:
  ```json
  {
    "deletion_sku_mode": 1,
    "cluster_info": {
      "macrolocal_cluster_id": 4071,
      "items": [{"sku": 12345, "quantity": 5}]
    }
  }
  ```
- `macrolocal_cluster_id` ≠ `id` (в `/v1/cluster/list` теперь оба поля, надо брать macrolocal).
- Ответ синхронный (`draft_id` сразу, без polling). Помечаем как `sync:<id>` чтобы handler знал не делать polling.
- Парсим `errors[]` в ответе → `OzonAPIError` с понятным текстом.

**Файлы:** `src/integrations/ozon_api.py`, `src/bot/handlers/ozon_book.py`.

### UX-рефактор
- `/start` — главное меню инлайн-кнопками («📋 Мои заявки» / «🔗 Привязать каталог» / «🔌 Проверить API» / «📚 Справка»).
- `/help` — короткая структурированная справка с кнопкой «🏠 Главное меню».
- `/ship` — список заявок сгруппирован: 🟣 WB / 🔵 Ozon / 🟡 Смешанные, кнопки на каждую заявку (не команды).
- Карточка заявки `_render_request_card`:
  - draft → «🛠 Спланировать даты»
  - planning → разделено по MP: «🔍 Подобрать склад WB» (только если есть WB), «🚀 Создать поставку Ozon» (только если есть Ozon)
  - URL-кнопки «🌐 WB ЛК → Поставки» и «🌐 Ozon ЛК → Поставки» (deep links)
  - Снизу: «📤 ТЗ xlsx» / «📎 + Файл» / «◀ К списку» / «🗑 Удалить»
- `edit_text` вместо `answer` везде где возможно (меню, пагинация ханта, открытие карточки).
- `/ship_hunt` — плоский paginated список (5 на стр.) с навигацией ◀ / ▶, кнопки подписаны: `🟢 Склад · 15.05 Пт · 100%`.
- Старые `/supply_*` и `/export` команды убраны из роутера (`src/bot/main.py`).
- В `upload.py` фильтр заявок по marketplace при загрузке файла — WB-файл предлагается только к WB-заявкам.

### WB деливери коэффициент
- WB API `acceptance/options` возвращает `deliveryCoef` уже в процентах (125 = 125%), а я раньше умножал на 100 → было 12500%. Убрал умножение в `src/bot/handlers/integrations.py:287-292` и `src/bot/handlers/shipment.py:719-725`.

### Строгий food-фильтр WB складов
- В `src/services/slot_hunter.py`: для матчинга API-склада с food-таргетом требуем чтобы **все слова таргета** (включая «Питание») были в имени склада. Иначе «Рязань (Тюшевское)» (не-food) подмешивался к таргету «Рязань Тюшевское: Питание».

### Кросс-док выпилен из UX
- Через WB API создавать поставку нельзя — кросс-док для WB бесполезен.
- Через Ozon API CROSSDOCK работает, но мы пока сделали только DIRECT (упростили wizard, всегда `CREATE_TYPE_DIRECT`).
- `_ask_crossdock_mode` в `src/bot/handlers/shipment.py` теперь сразу зовёт `_show_confirm` без вопросов.

### Прокси Ozon (`OZON_PROXY_URL` в .env)
- Пробовали `45.139.171.188:8000` — TCP проходит, ответ не возвращается. Закомментировано в `.env`.
- Поддержка прокси оставлена в коде: `OzonClient(.., proxy=OZON_PROXY_URL)` принимает строку формата `http://user:pass@host:port`.

**Статус:** Часть рабочая (draft создаётся), `timeslot/info` упирается в 429 — этим занимаемся в следующей итерации (см. запись сверху от 09:24).

---

## История до 2026-05-12 07:30

См. `план_автоматизации_ФФ.docx` (стратегия) и `C:\Users\vladi\.claude\plans\moonlit-booping-dream.md` (MVP-план). Начальная разработка: каркас бота, БД-схема, парсеры (опись WB/Ozon, prihod, ostatki), генераторы ТЗ Приёмка/Отгрузка, sku-линкинг к маркетплейсам, slot_hunter для WB, shipment_requests модель.
