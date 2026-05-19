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

## 2026-05-19 — Большая сессия: 7 полировок + охота на 429 + orphan-items бомба

### Коммиты в main (по порядку):

1. **77e294a** — polish 7 UX/diag fixes + parallel pipeline (включил старый эксперимент)
   - #1 ТЗ Отгрузки «поставка не найдена»: `cb_ship_tz` теперь берёт `tg_id=cb.from_user.id` (раньше `msg.from_user.id` = бот) — shipment.py:2225-2235.
   - #2 Scoring «Попытка 1/4: не удалось»: pre-sleep 7с + новые формулировки («Жду расчёт Ozon», «Ozon ещё считает») — ozon_book.py:1422+.
   - #3 Diag `/v1/draft/create` → `/v1/draft/crossdock/create` (после 16.03.2026 v1/draft/create отключён) — integrations.py:75-99.
   - #6 Финал bulk-book: `edit_message_text(ob_progress_msg_id)` вместо нового сообщения — ozon_book.py:4112-4163.
   - #7 Утренняя сводка: helper `_sep(lines) = ["", "━━━━", ""]` между секциями + убран список с пометкой «Открыть в Ozon ЛК», пояснение «через 5 дней» — digest_service.py.
   - #5 ЛК-поставки: URL-кнопки → callback `ext_open:`/`ext_tz:`. Карточка через `supply_order_get`, ТЗ через новый `supply_order_bundle` + `generate_tz_otgruzka` — shipment.py:166-460.

2. **e6e9802** — hotfix 429 #1: добавил `/v1/draft/{crossdock,direct,multi-cluster}/create` в `_GLOBAL_LIMIT_PATHS` (5 ретраев backoff 2-5с вместо 1×30с).

3. **667355d** — heavy backoff 5-45с × 6 (~125с окно) — не помогло: 7 попыток подряд 429.

4. **0b9a43a** — откат к 8с pacing serial + UX (убрал ссылку в сводке, добавил пустую строку перед списком актов).

5. **e881dc8** — heavy retries 6→2 (~60с окно). Гипотеза: наши собственные ретраи участвовали в перегрузке лимита.

6. **8380370** — точечный откат `_auto_book_explore` к `e5bee76`-варианту (sequential `for`, 5с pause, log_lines). Удалил параллельный pipeline целиком.

7. **abc9fd2** — heavy 429 → cooldown 5 мин: после исчерпанных ретраев на draft/*/create ставим persistent cooldown на path. Бот не дёргает Ozon, юзер видит сообщение про габариты в Seller Center.

8. **3326101** — UX cleanup: финал bulk-book учитывает `ob_booked_clusters` (счётчик «N из M» включает ранее забронированные); убрал строку «✅ Scoring готов» (вводило в заблуждение перед NO_TIMESLOTS); убрал «✅ Drop-off-точки выбраны для N кластеров»; убрал отдельный экран `ship_pick_fmt` (формат `BOX` дефолтится).

9. **5c52ba8** — PRAGMA foreign_keys=ON для SQLite (db/session.py). **Корень главного бага сессии**: bulk SQL `DELETE FROM shipment_requests` оставлял orphan ShipmentItem — SQLite по умолчанию игнорит `ondelete='CASCADE'`. При переиспользовании rid=1 новая заявка «наследовала» orphans с гигантскими qty (HALLOWEEN-40G × 657687) → Ozon отвергал draft `TOTAL_VOLUME_IN_LITRES_INVALID`. После включения PRAGMA cascade работает на уровне БД.

### Прокси:
- Старый SOCKS5 закончился (исчерпал баланс).
- Новый: `socks5://BxGKJ9:W7Phq5@45.133.225.67:8000`.
- В `.env` на VPS обновлён. Бот стартовал с ним, `cluster_list` → 22 кластера ОК.

### Чистки БД:
- Удалены заявки tg_id=1092120960 (0 заявок было).
- Удалены 9 заявок tg_id=6009938595 (77 items, 2 drafts) + 90 orphan items (от не-cascade DELETE).
- `.env`: `ADMIN_USER_IDS=1092120960,6009938595` → `ADMIN_USER_IDS=1092120960` (второй ID убран по команде юзера).

### Что НЕ закрыто (открытые вопросы):

1. **429 на 3-м кластере** в `_create_drafts_and_fetch_scoring_inner` (ozon_book.py:1741 — pacing 15с). Даже 15с не хватает — Ozon лимитит этот аккаунт жёстко. Cooldown 5 мин работает как safety, но UX страдает. Возможные направления:
   - Увеличить pacing до 25-30с (-UX).
   - Переключить на batch-режим: создаём 1-2 кластера → ждём 1 мин → следующие.
   - Использовать `last_draft_create_ts` глобально — не выходить раньше, чем прошло N сек с прошлого draft_create на любой кластер.

2. **«Нет таймслотов» формулировка**: бот пишет «у drop-off-точки нет таймслотов» когда timeslot/info вернул пусто. Реальная причина может быть разная (хаб не возит сюда / нет окон в эти даты / scoring отбраковал). Стоит детализировать.

3. **Дубликат заявок rid=7 + rid=8**: один и тот же контент с разницей в минуту — юзер дважды нажал «загрузить». Можно сделать дедупликацию по `source_files_json[0] + user_id + last 1 min`.

4. **Утечка поставок «из второго кабинета»**: юзер пожаловался, но при проверке БД утечки нет (legacy `user_id IS NULL` = 0, Client-Id у двух tg_id разные). Если повторится — нужен конкретный rid + tg_id.

5. **Габариты SKU в Ozon SC**: для некоторых товаров возможно реально не заполнены. Дёрнули `/v4/product/info/attributes` — три проверенных SKU имели depth/height/width/weight. Если ошибка `VOLUME_INVALID` повторится после чистки orphans — это другая история (большие qty в одной коробке или хабовый лимит).

### Что точно работает после сессии:
- ТЗ Отгрузки скачивается (`ship_tz`).
- ЛК-поставки открываются как карточка с кнопкой «📤 Скачать ТЗ Отгрузки».
- Финал bulk-book — едитит сардельку, кнопки в ней (max-edit принцип).
- Сводка — разделители `━━━━` между секциями, без «Открыть в Ozon ЛК».
- Скоринг сообщения — нейтральные («Жду расчёт Ozon…»), без «Попытка 1/4: не удалось» в первые 7с.
- Diag дёргает актуальный `/v1/draft/crossdock/create`.
- Сараделька на финале не дублируется новым сообщением.
- 429 → 2 ретрая 20+40с → cooldown 5 мин.
- Cascade удаление ShipmentItem при удалении ShipmentRequest (даже через bulk SQL).

## 2026-05-16 (13:30) — авто-брон Ozon финал: 3-кнопочный режим + auto-poll сам бронит + UX wizard

Длинный спринт (~3 часа) — закончили Фазу 3 (auto-book) end-to-end, плюс полировка UX обычного wizard'а. **HEAD `7ceb0d6` — стабильная рабочая версия на проде.**

### Что готово (полный flow)

**Создание поставки (новый UX):**
1. Юзер кидает xlsx → бот парсит.
2. **Спрашивает тип Ozon** (Прямая / Кросс-докинг) ДО создания заявки.
3. Создаётся ShipmentRequest с типом, открывается карточка.
4. Юзер жмёт `🛠 Спланировать даты` → picker дат + picker часов.
5. **После часов** — 3 inline-кнопки:
   - `🎯 В одну дату` — найти best_date с макс. кластеров, бронить любое время.
   - `🎯 В одно время` — best_date + best_hour (синхрон).
   - `🛠 Ручной` — обычный wizard.
6. После выбора — auto_book_mode сохраняется в БД (виден в карточке).

**Авто-брон pipeline (🎯):**
- Создаёт draft per кластер с пауза 8с между (Ozon /v2/draft/create/info per-second limit).
- Ждёт scoring (5 попыток × 3с — для DIRECT, top-1 wh из FULL_AVAILABLE).
- Дёргает draft_timeslot_info с диапазоном дат.
- `find_best_common_date` (auto_book.py) — выбирает оптимальную.
- Сразу bulk-book: для каждого кластера со слотом на best_date → `_book_one_slot`.
- Для кластеров **без** слота на best_date → spawn `_auto_poll_slots` с `auto_book=True`.

**Auto-poll auto-book режим:**
- Каждые 60с дёргает timeslot/info для каждого draft.
- При нахождении слота → `_silent_book_slot` (упрощённый без state/UI) бронит сам.
- Шлёт «🔔 Авто-брон #N: Самара забронирована! 📌 21.05 18:00».
- Через 60 мин если ничего не нашлось — «😔 за час не сложилось».
- Recovery: на 404 «scoring not found» >180с — пробуем пересоздать draft.

**CROSSDOCK особенности:**
- При `🎯 В одну дату` для CROSSDOCK без drop-off в БД → **сразу** перекидываем в карточку поставки (без промежуточного сообщения). Юзер кликает обычный «🚛 Создать поставку Ozon», проходит drop-off picker → drop-off сохраняется в БД через `_ask_dropoff_for_next_cluster` (commit a1e8268).
- При повторном клике `🎯` — уже работает.

**UX wizard'а (обычного, не auto):**
- «draft» / «scoring» → «расчёт» / «Ozon считает варианты».
- Между попытками scoring — чёткие строки «попытка X/4: Ozon ещё считает. Жду Yс до попытки X+1/4».
- В финале: «❌ Не удалось за 4 попытки. 🔄 Перехожу к следующему кластеру».
- Bulk-book заголовок: «X из Y (ещё Z в авто-поиске)».
- Bulk-book финал: «✅ Забронировано X/Y · ⏳ Z ищу слот ещё час».
- Failed-scoring кластеры явно в finale.
- Пауза между кластерами 60→5с (rate-limit Ozon 2 req/sec).

### Файлы (новые/изменённые)

**Новые:**
- `src/services/auto_book.py` (185 строк) — `find_best_common_date`, `parse_timeslot_response`, `clusters_with_slots_on`, `pick_earliest_slot`, `date_options_summary`.
- `tests/test_auto_book.py` (166 строк, 14 unit-тестов, все зелёные).
- `alembic/versions/a92f1c5d7e84_add_auto_book_mode.py` — миграция.

**Изменённые:**
- `src/db/models.py` — `ShipmentRequest.auto_book_mode VARCHAR(8) NULLABLE`.
- `src/bot/handlers/ozon_book.py` (+~900 строк): `cb_obauto`, `_auto_book_explore`, `_auto_run_bulk`, `_silent_book_slot`, `_spawn_auto_poll_for_best_date`, `cb_obauto_book_day`, `cb_obauto_book_hour`. UX wizard'а — пересмотр текстов.
- `src/bot/handlers/shipment.py` — кнопка `🎯` (потом убрана из карточки), 3-кнопочный экран после ship_plan, `kind="wide"` в `cb_up_otype`, drop-off в БД.
- `src/bot/handlers/upload.py` — `_handle_wide_ship_file` спрашивает тип ДО create_shipment_request.
- `src/integrations/ozon_api.py` — добавлен метод `analytics_stocks` (для digest, не auto-book).

### Статус
- HEAD: `7ceb0d6` на проде через NL-прокси.
- Бот active, NRestarts стабильно 0.
- 17 unit-тестов passed (14 новых + 3 старых).
- Telegram через прокси `socks5://...@168.80.80.109:8000` (NL, аренда на неделю — закончится ~22.05).

### Open issues / следующие задачи

1. **Параллелизация авто-брон** (юзер очень хочет, обсуждали детально):
   - Сейчас `for cl in clusters` синхронно: 4 кластера × ~3 мин = ~12 мин.
   - Параллельно через `asyncio.gather` + semaphore(2) для draft_create: ~4 мин.
   - Реализуемо в `_auto_book_explore`. ~150 строк. **Главная следующая задача.**

2. **Multi-drop-off fallback для CROSSDOCK** (после параллелизации):
   - Сейчас auto-poll сидит на одной точке кросс-дока (Щербинка). Если не даёт слотов → ничего.
   - Юзер выбирает 2-3 «точки кросс-докинга» (НЕ «drop-off» в UI — переименовать!).
   - Бот перебирает их при провале. С параллелизацией: ~9 мин на 4 кластера × 3 точки.

3. **Переименование UI**: «drop-off» → «точка кросс-докинга». Юзер: «дроп-офф это не так называется».
   - Файлы: `ozon_book.py` (_ask_dropoff_for_next_cluster), `favorites.py`, `auto_book` messages.
   - В **коде** оставить `drop_off_*` (это термин Ozon API).

4. **Multi-warehouse fallback для DIRECT auto-poll**:
   - Сейчас auto-poll бьёт в один wh_id. Если 404 → ждёт час впустую.
   - Сделать топ-3 wh из scoring. ~40-60 строк.

5. **Pre-check ассортимента** (low priority):
   - `/v1/analytics/stocks` per-cluster данные → знать заранее что SKU не торгуется в кластере.
   - Не создавать заведомо мёртвые drafts.

6. **Smart-selection точек кросс-дока** (после сбора истории):
   - Логировать (drop_off, cluster) → success/fail.
   - Через 1-2 нед использования — выбирать «исторически лучшую» точку для кластера.

### Запуск с новой сессии
1. Прочитать `memory/project_current_focus.md` (актуальный TODO).
2. Прочитать `C:\Users\vladi\.claude\plans\smart-supply-booking.md` (большой план).
3. **Главный приоритет — параллелизация авто-брон** (#1 выше).

---

## 2026-05-16 (11:30) — авто-брон Ozon: foundation + UI explore-этап

### Зачем
Большая фича для Ozon-поставок: бот сам ищет оптимальную дату (где максимум кластеров может уехать) и далее планирует bulk-book + auto-poll для оставшихся в течение часа. План — `C:\Users\vladi\.claude\plans\smart-supply-booking.md`.

### Что сделано (Фаза 3 шаги 1-2)
- **Фаза 0 (исследование)**: на проде дёрнули `/v2/draft/create/info` и `/v1/draft/timeslot/info` для тестовых SKU. Выяснили:
  - `timeslot/info` отдаёт **все слоты на 7+ дней одним запросом** (`days[].timeslots[]`) — не нужно дёргать per-date.
  - `scoring` готов за 3-78 секунд (asynchron, нужен retry).
  - `availability_status.state = FULL_AVAILABLE / NOT_AVAILABLE` per warehouse.
- **`src/services/auto_book.py`** (185 строк) headless-модуль с алгоритмом:
  - `SlotInfo` dataclass.
  - `parse_timeslot_response(json) → List[SlotInfo]`.
  - `find_best_common_date(slots_per_cluster, allowed_dates, allowed_hours) → date | None` — главный алгоритм. tie-break по ранней дате.
  - `clusters_with_slots_on`, `pick_earliest_slot`, `date_options_summary`.
- **`tests/test_auto_book.py`** — 14 unit-тестов, все зелёные на VPS. Edge-cases: пустые слоты, разные даты, tie-break, real Ozon response structure.
- **Smoke на проде**: Москва + Самара, 5 шт sku 1607482374 → 187/204 слотов на 9 дат → best_date=2026-05-16 (2/2 кластеров). Алгоритм работает.
- **UI кнопка**: в `_render_request_card` добавлена `🎯 Авто-брон на одну дату` рядом с обычной «Создать поставку Ozon». Показывается для DIRECT и CROSSDOCK когда тип Ozon уже задан.
- **`cb_obauto` + `_auto_book_explore`** в `ozon_book.py` (~250 строк):
  - Создаёт drafts per кластер последовательно (с паузой 5с).
  - Ждёт scoring (5 попыток × 3с).
  - Берёт top-1 склад из FULL_AVAILABLE.
  - Дёргает `timeslot/info` с диапазоном дат.
  - Применяет `find_best_common_date`.
  - Показывает результат через `edit_text` одного сообщения.

### Не сделано (Фаза 3 шаги 3-5 — bulk-book)
- Callback `obauto:confirm:rid:date` → bulk-book на best_date.
- Auto-poll для кластеров без слотов на best_date.
- Фоновые уведомления юзера когда auto-poll забронировал.

### Известные баги (см. project_current_focus)
1. **CROSSDOCK drop-off не сохраняется в БД** — авто-брон не работает на cross-dock.
2. **`_run_bulk_book` summary врёт «1 успешно»** при 1 из 3 кластеров с провалом scoring — failed-кластеры не попадают в финальный отчёт.
3. **Wizard UX**: «draft» / «scoring» — техжаргон. Юзер не понимает.
4. **Регрессия исправлена** (`f554822`): `cb_ship_pick_fmt` в `shipment.py:1144` не передавал `tg_id` в `_start_ozon_book_wizard` после E4-миграции. После «Коробами/Паллетами» получали TypeError.

### Файлы
**Новые:**
- `src/services/auto_book.py`
- `tests/test_auto_book.py`

**Изменённые:**
- `src/bot/handlers/shipment.py` — кнопка + регрессия fix
- `src/bot/handlers/ozon_book.py` — handler `cb_obauto` + `_auto_book_explore` + `_resolve_macrolocal_id`
- `pyproject.toml` — добавлен aiohttp-socks/httpx-socks (раньше) — нет, это другой commit

### Статус
HEAD на проде `8b16a87`. Бот active. Кнопка видна, для DIRECT — работает.

---

## 2026-05-16 (07:00) — E4 завершение: handlers → get_ozon_client_for(tg_id)

### Зачем
На проде ловили дыру: второй юзер (tg_id=6009938595, без своих Ozon-ключей) делал `/sku_link_ozon` и тянул каталог из `.env`-кабинета Vladislav'а (ALLOWED_USER_ID), писал в `ozon_products` (legacy `user_id=None`), смешивал с данными владельца. Тот же баг был во всех handlers, где остался прямой `OzonClient(CLIENT_ID_OZON, APIKEY_OZON, …)`. E4 был запланирован в прошлой сессии, оставались ~17 callsite.

### Что сделано
- **`src/bot/handlers/integrations.py`** — все Ozon/WB-команды (`/api_check`, `/api_warmup`, `/ozon_diag`, `/ozon_stocks`, `/ozon_warehouses`, `/sku_link_ozon`, `/sku_link_wb`, `/wb_stocks`, `/wb_coefs`) — теперь читают `current_user_id_from(msg)` → `get_ozon_client_for(s, tg_id)` / `get_wb_api_key(s, tg_id)`. Если нет кред — `_NEED_OZON` / `_NEED_WB` ответ.
- **`/sku_link_ozon` + `/sku_link_wb`** — фикс главной утечки: existing-query фильтруется по `user_id == tg_id` (для ALLOWED_USER_ID — `+ user_id IS NULL` для legacy). При создании `OzonProduct` / `WbProduct` пишем `user_id=tg_id`. legacy-записи (`user_id=None`) подтягиваются и присваиваются текущему юзеру при следующем sync. Это закрывает «обновлено 9 артикулов в чужом каталоге».
- **`src/bot/handlers/ozon_book.py`** — все 7 callsite заменены на `_ozon_client_from_state(state)` (читает `ob_tg_id` из state) или `_ozon_client_for_tg(tg_id)`. Сигнатура `_start_ozon_book_wizard` теперь требует явный `tg_id`. Кошерный prepass — callers (`cmd_ozon_book`, `cb_ozon_book_from_card`, `cb_ozon_book_auto`) берут `current_user_id_from(...)` и передают. `_auto_poll_slots` получил параметр `tg_id`, фоновая задача создаёт OzonClient через него.
- **`src/bot/handlers/shipment.py`** — `cb_ship_open` (refresh status), `cb_ship_new_template`, `cb_ship_cancel_oz_confirm`, `cb_ship_refresh_oz`, `_run_hunt` (Ozon+WB), `_send_ship_tz`, `cb_clear_drafts` — все через `current_user_id_from(cb/msg)` → `get_ozon_client_for`. Если нет кред — early return с `_NO_OZON_KEYS_MSG_SHIP`.
- **`src/bot/handlers/favorites.py`** — `_search_warehouses(query, *, tg_id)`, `_resolve_warehouse_name(wh_id, *, tg_id)` — оба требуют tg_id. Callers (`msg_fav_add_query`, `cb_fav_pick`, `msg_obdo_input` в ozon_book) пробрасывают tg_id из event / state.
- **`src/bot/handlers/returns.py`** — `cb_ret_wb` тоже мигрирован на `get_wb_api_key(s, tg_id)` (хотя WB скрыт из меню после E5, доступ через старые callbacks возможен).
- **Тексты** — `f"В Ozon (client_id={CLIENT_ID_OZON})"` в `ozon_book` заменено на «В твоём Ozon-кабинете» (не светим client_id ALLOWED_USER_ID).

### Статус
- `python -m pytest -x -q` → 3 passed, 8 skipped.
- Все handlers импортируются (`python -c "import …"` OK).
- Лучшее на проде, чем оставлять дыру.

### TODO для следующей сессии
- Сменить пароль root на VPS (был в чате — паролю кранты).
- Digest UI: юзер просит **топ-5 урезать**, сейчас «каша» при большом каталоге. См. скрин `data/screenshots/photo_2026-05-16_06-46-13.jpg` если перекинул. Поправить в `digest_service._build_lines` + `build_digest_text` — лимит топ-5 на urgent и runout, цветные группы свернуть.
- E2E-тест: с второго аккаунта пройти `/start` → `/sku_link_ozon` без своих кред — должен получить «Сначала /start», а не лезть в `.env`-кабинет.

---

## 2026-05-15 (14:00) — Multi-tenant MVP + production hardening (E1-E6)

### Зачем
Юзер хочет публичный запуск с максимальным заделом по защите перед деплоем на VPS. План — `C:\Users\vladi\.claude\plans\mighty-squishing-dream.md` (одобрен).

### Что готово (по этапам плана)

**E1: Изоляция БД**
- Модели: `OzonProduct.offer_id` / `WbProduct.nm_id` сняты с глобального `unique=True`, переведены в composite `UNIQUE(user_id, offer_id)` / `UNIQUE(user_id, nm_id)`. `FavoriteCrossdockPoint` получил `UNIQUE(user_id, warehouse_id)`.
- Миграция `b8d2e7a91f02_composite_unique_user_data.py` (batch_alter_table под SQLite).
- `src/services/user_service.py`: добавлены `current_user_id_from(event)`, `get_owned(session, model, id, user_id)` (cross-tenant safe get), `get_ozon_client_for(session, tg_id)` (helper для handlers).
- `src/services/shipment_service.py`: все функции (`_find_ozon_product`, `_find_wb_product`, `create_shipment_request`, `attach_ship_file`, `list_shipment_requests`, `get_shipment_request`) принимают `user_id: Optional[int] = None`. Backward-compat: если None — fallback на `ALLOWED_USER_ID`. Все queries обёрнуты в `_user_filter(query, model, user_id)` который добавляет `(Model.user_id == uid) | (Model.user_id.is_(None))` для legacy-записей при uid==ALLOWED_USER_ID.
- `src/services/product_hint_service.py`: то же — `user_id` опциональный, фильтрация через `_owned_product_filter` JOIN на OzonProduct.
- **Важно**: handlers ВСЕ ЕЩЁ вызывают сервисы без `user_id` → fallback на ALLOWED_USER_ID. Для текущего single-tenant работает. Для multi-tenant **handlers надо мигрировать** (TODO ниже).

**E2: Fernet шифрование токенов**
- `src/security/crypto.py` — `encrypt`/`decrypt`/`is_encrypted`. Master-key из env `TOKEN_ENCRYPTION_KEY`. Без ключа — plain mode + WARN в лог (dev-режим). Идемпотентно: повторный encrypt не двойным кодом (детект префикса `gAAAAAB`).
- `src/services/user_service.save_ozon_creds` / `save_wb_creds` оборачивают токен в `encrypt()` перед записью. `get_ozon_creds` / `get_wb_api_key` — `decrypt()` при чтении. На decryption error — лог + None (юзер заново вводит).
- Миграция `c4f2e9b1a3d8_encrypt_user_tokens.py` — идемпотентно encrypt-ит существующие plain-токены если ключ задан в env. No-op без ключа.
- `src/config.py`: добавлена `TOKEN_ENCRYPTION_KEY = os.getenv("TOKEN_ENCRYPTION_KEY", "")`.

**E3: Открытый доступ + rate-limit + abuse-protection**
- `OnlyAllowedUser` фильтр в `main.py` — превращён в no-op (`return True`). Бот теперь публичный.
- `src/bot/middleware.py`:
  - `EnsureUserMiddleware` — на каждое event делает `get_or_create_user(s, tg_id)` чтобы запись в `users` всегда была.
  - `RateLimitMiddleware(max_per_min=30, max_per_hour=200)` — in-memory, deque per tg_id. Превышение → ответ «Слишком часто» + drop.
  - `LogAndCatchMiddleware._looks_like_secret(text)` — маскирует длинные hex/uuid строки в логах (защита от утечки токенов в `bot.log` при onboarding).
- `_digest_scheduler` итерируется по `_get_digest_recipients()` — все юзеры с `onboarded_at != None` + ALLOWED_USER_ID. Между юзерами `sleep(2)` для Telegram rate-limit.
- `src/bot/handlers/common.py`:
  - `/forget_me` — двухшаговое подтверждение → каскадно удаляет User (FK CASCADE прибивает заявки/каталог/favorites/hints).
  - Inline-кнопка «🗑 Удалить мои данные» в `_settings_menu_kb`.
  - `/admin_stats` — только для ALLOWED_USER_ID. Показывает total users / onboarded / заявок 7д / заявок всего.
- `main.py`: middleware'ы зарегистрированы в `dp.update.outer_middleware` (порядок: log → ensure_user → rate_limit).

**E4: Handlers → user_service.get_ozon_client_for** (частично)
- Helper `get_ozon_client_for(session, tg_id)` создан, возвращает готовый OzonClient или None.
- Мигрированы: `digest.py`, `returns.py` (Ozon-блок).
- **НЕ мигрированы** (~17 callsite, отложено на следующую сессию): `ozon_book.py` (10 мест), `integrations.py` (5), `shipment.py` (5 локальных импортов), `favorites.py` (2). Backward-compat работает через `user_service.get_ozon_creds` fallback на .env для ALLOWED_USER_ID. **Дыра для multi-tenant**: новый юзер, который введёт свои Ozon-ключи, не сможет создать поставку через ozon_book — оно будет лазить в .env-кабинет ALLOWED_USER_ID. Нужно завершить.

**E5: Скрыть WB из UI**
- `keyboards.py:kb_marketplace()` — без WB-кнопки.
- `common.py:_settings_menu_kb` — без «📊 WB коэффициенты». Sku_link меню (`_sku_link_kb`) — только Ozon.
- `cb_menu_returns` — без «🟣 Wildberries» кнопки.
- `_MAIN_TEXT` / `_HELP_TEXT` — без упоминания WB как активной фичи. Внизу Help — пометка «WB пока в разработке».
- WB-handlers (`cb_ret_wb`, WB sku_link) НЕ удалены — просто скрыты из меню.

**E6: Production hardening**
- `src/bot/handlers/upload.py` — лимит размера файла 20 МБ (защита от accidental DoS).
- `src/bot/main.py:run()` — try/except KeyboardInterrupt+SystemExit для graceful shutdown.
- `deploy/postavka-bot.service` — systemd unit с hardening (NoNewPrivileges, ProtectSystem=strict, PrivateTmp). User=postavka, Restart=always, SIGTERM, journald.
- `deploy/backup.sh` — daily SQLite-safe бэкап через `sqlite3 .backup`, ротация 14 дней.
- `deploy/README.md` — пошаговая инструкция деплоя на чистую Ubuntu (useradd, venv, alembic upgrade, systemd, cron).
- `.env.example` — все актуальные переменные с комментариями (TELEGRAM_BOT_TOKEN, ALLOWED_USER_ID, TOKEN_ENCRYPTION_KEY, legacy fallback'и).

### Файлы (новые/изменённые)

**Новые:**
- `src/security/__init__.py`, `src/security/crypto.py`
- `alembic/versions/b8d2e7a91f02_composite_unique_user_data.py`
- `alembic/versions/c4f2e9b1a3d8_encrypt_user_tokens.py`
- `deploy/postavka-bot.service`, `deploy/backup.sh`, `deploy/README.md`
- `.env.example`

**Изменённые:**
- `src/db/models.py` — composite UNIQUE
- `src/services/user_service.py` — Fernet, helpers, get_ozon_client_for
- `src/services/shipment_service.py` — multi-tenant filter
- `src/services/product_hint_service.py` — multi-tenant filter
- `src/bot/middleware.py` — EnsureUserMiddleware, RateLimitMiddleware, секрет-маскирование
- `src/bot/main.py` — middleware'ы, scheduler по всем юзерам, run() shutdown
- `src/bot/keyboards.py` — без WB
- `src/bot/handlers/common.py` — _MAIN_TEXT/_HELP_TEXT, _settings_menu_kb, /forget_me, /admin_stats
- `src/bot/handlers/digest.py` — get_ozon_client_for
- `src/bot/handlers/returns.py` — get_ozon_client_for
- `src/bot/handlers/upload.py` — лимит 20 МБ
- `src/config.py` — TOKEN_ENCRYPTION_KEY

### Статус
Бот рестартован, работает (`Bot started`, `DIGEST scheduler: sleep 67925 сек до 16.05 09:00 МСК`). Миграции накатаны (head: `c4f2e9b1a3d8`).

### TODO для следующей сессии
1. **E4 завершить**: мигрировать `ozon_book.py` (10 мест), `integrations.py`, `shipment.py`, `favorites.py` на `get_ozon_client_for(s, tg_id)`. Это критично для multi-tenant — без этого новые юзеры будут писать в кабинет ALLOWED_USER_ID.
2. Сгенерировать TOKEN_ENCRYPTION_KEY в .env перед первым деплоем + `alembic upgrade` чтобы зашифровать существующие токены.
3. (опционально) Добавить in-БД bad_creds_count + creds_blocked_until для API abuse-protection (был в плане E3.5, отложил).
4. (опционально) HTTP `/healthz` endpoint для systemd/k8s healthcheck.
5. End-to-end тест: создать второго Telegram-аккаунта, пройти /start → onboarding → попытаться создать заявку. Должно: 1) видеть только свой каталог 2) не видеть заявки ALLOWED_USER_ID 3) после `/forget_me` всё стереть.

---

## 2026-05-15 (13:25) — Утренняя сводка + /digest (возвраты, urgent, runout)

### Зачем
Юзер хочет каждое утро видеть короткий one-glance-отчёт по кабинету Ozon без клика в ЛК. По интервью с Маргаритой (см. memory `project_interview_margarita_2026_05_15`) это закрывает три топ-боли: уведомления о возвратах, «срочно отгрузить», «когда кончится». Триггер двойной — расписание 09:00 МСК и команда `/digest` на ручной вызов / для отладки.

### Что добавлено

- **Метод API**: `OzonClient.postings_fbo_list(date_from, date_to, status="", limit_per_page=1000, max_total=20000)` — заказы FBO за период с пагинацией через offset, endpoint `/v2/posting/fbo/list` (deprecated с 1.06.2026, до тех пор живой). Без `analytics_data`/`financial_data` для скорости — нужен только `products[{sku, qty}]` и `status` для отсеки cancelled.
- **`src/services/digest_service.py`** — sole логика сборки:
  - `collect_returns_summary(oz)` → total + actionable_at_pvz + giveouts_available/at_pvz + pdf_bytes (None если нет).
  - `collect_sales_and_stocks(oz, days_window=28)` → `{sku: {stock, sold_7d, sold_28d, name, offer_id}}`. Источник `name`/`offer_id` — сначала stocks_fbo, потом fallback из products в постингах. Cancelled posting'и пропускаем. Sold_7d считаем по `in_process_at >= now-7d`.
  - `_build_lines(by_sku, mode='urgent'|'runout')` считает rate и days_left, фильтрует по color-порогам:
    - urgent: rate=sold_7d/7, 🔴 <7д, 🟡 7–14д, остальное скрываем
    - runout: rate=sold_28d/28, 🔴 <15д, 🟡 15–30д, 🟢 >30д
  - `build_digest_text(data)` собирает HTML: возвраты → 🔥 срочно → ⏳ runout → ⚠ ошибки API (если были).
- **Handler** `src/bot/handlers/digest.py`:
  - `send_digest_to_user(bot, chat_id)` — reusable, шлёт PDF (если есть) отдельным document'ом, потом текст с кнопкой «🔄 Обновить».
  - `/digest` команда — ручной триггер с placeholder-сообщением.
  - `digest:refresh` callback — пересборка по кнопке.
- **Scheduler** в `main.py`: `_digest_scheduler(bot)` — asyncio loop, спит до ближайшего 09:00 МСК (UTC+3), потом шлёт. APScheduler не тащил — single-tenant, asyncio.sleep справляется. На исключении лог + sleep час, чтобы не ушло в tight-loop. Запускается через `asyncio.create_task` параллельно с `start_polling`, отменяется в finally.
- **Меню команд**: добавлен `BotCommand("digest", "☀ Утренняя сводка")`.

### Файлы
- `src/integrations/ozon_api.py` — добавлен `postings_fbo_list` (перед `supply_order_*`).
- `src/services/digest_service.py` — новый.
- `src/bot/handlers/digest.py` — новый.
- `src/bot/main.py` — импорт digest, регистрация router'а, `_digest_scheduler` корутина, `set_my_commands`, оборачивание `start_polling` в try/finally для cancel scheduler'а.

### Статус
Импорт-смоук ОК, бот рестартован (`Bot started: @my_postavka_bot`, scheduler залогировал «sleep 70571 сек до 16.05 09:00 МСК»). Реальный `/digest` юзер ещё не дёргал — следующий шаг. Если postings_fbo_list упрётся в 429 — увидим в `data.errors` блоке сводки.

### Дефолты, которые поправим если юзер скажет
- Время 09:00 МСК — константы `DIGEST_HOUR_MSK`/`DIGEST_MINUTE_MSK` в `main.py`.
- Пороги urgent/runout — `URGENT_RED_DAYS=7`, `URGENT_YELLOW_DAYS=14`, `RUNOUT_RED_DAYS=15`, `RUNOUT_GREEN_DAYS=30` в `digest_service.py`.
- TOP_LIST_LIMIT=12 — после которого пишем «…и ещё N».

### Будущее
- **Следующая задача (юзер уже описал)**: фикс поиска слотов Ozon — искать на ближайшую дату, но **последний слот этого дня**. Локация: `ozon_book.py` + возможно `slot_hunter.py`. Не начинать пока юзер не скажет.
- Кнопка «☀ Сводка сейчас» в главное меню (если юзер захочет — пока только команда + /digest).
- Тренд: сравнение с прошлой неделей (нужна история заказов в БД, сейчас live).
- Алерты на новые слоты Ozon FBO в утренней сводке (юзер упомянул как боль #1).

---

## 2026-05-15 (07:30) — ProductHint: загрузка упаковки/примечаний через xlsx → в ТЗ Отгрузка

### Зачем
Юзер хочет один раз залить таблицу с упаковкой/примечаниями на каждый артикул, и чтобы при генерации ТЗ Отгрузка эти поля автоматически попадали в xlsx-выпуск. В `ship_tz.py` колонки «Упаковка для товара» / «примечание» уже есть в шапке (`:201-208`), но всегда писались `None` (`:263-264`).

### Архитектурный выбор: хранение по product_id, а не offer_id
`offer_id` (артикул на Ozon) изменяемый — юзер может его поменять в ЛК. Стабильный ключ — числовой `sku`. Поэтому в `ProductHint` храним `ozon_product_id` (FK на `ozon_products.id`, который в свою очередь привязан к sku через `/refresh_ozon_catalog`). Юзер заливает по offer_id (что у него в xlsx), резолвим в product_id один раз → даже если артикул сменится, hint останется привязан к товару.

### Что добавлено

- **Модель** `ProductHint` (`src/db/models.py`): `ozon_product_id` FK UNIQUE, `packaging`, `notes`, `created_at`, `updated_at`. Миграция `e799858ae69c`.
- **Парсер** `src/parsers/product_hints.py`: читает xlsx, ищет колонки артикул/упаковка/примечание (case-insensitive, по подстроке: `артикул`/`offer`/`article`, `упаков`, `примеч`/`коммент`/`note`). Возвращает `list[HintRow]`. Без колонки артикула → ValueError.
- **Сервис** `src/services/product_hint_service.py`: `get_catalog_stats()` (total/with_hint/without_hint), `resolve_rows(rows)` (через существующий `_find_ozon_product` → matched + unmatched_articles), `apply_upsert(matched)` (обновляет только эти product_id), `apply_replace(matched)` (чистит таблицу и заливает заново). Дополнительно `get_hints_by_product_ids` для бэтча.
- **Handler** `src/bot/handlers/product_hints.py` + state `ProductHints` (awaiting_file, confirm_strategy):
  - `menu:product_hints` → pre-upload экран со счётчиками «всего N / с упаковкой K / без M». Если каталог пуст → подсказка «прогони /refresh_ozon_catalog», кнопка загрузки скрыта.
  - `phints:upload` → state.awaiting_file, юзер кидает xlsx.
  - `@router.message(ProductHints.awaiting_file, F.document)` — приём файла. Сохранение в `STORAGE_DIR/hints_*`. Парсинг → резолв → state.confirm_strategy.
  - Сводка: «✅ N распознано / ⚠ M не нашлись: ART-1, ART-2 …». Кнопки **🔄 Upsert (рекомендую)** / **🚮 Заменить всё** / **✖ Отмена**.
  - После применения — финальный экран с обновлёнными счётчиками и кнопками «📤 Залить ещё файл» / «◀ В меню».
- **Меню**: в `_main_menu_kb` добавлен пункт «📦 Упаковка / примечания» (`common.py`).
- **Регистрация роутера** в `main.py`: `product_hints.router` идёт **перед** `upload.router` — чтобы state-handler перехватил xlsx до катч-олла в `upload.py`.

### Подцеп к ТЗ Отгрузка
`src/generators/ship_tz.py`:
- В `generate_ship_tz` через `object_session(req)` берём активную SQLAlchemy сессию (caller вызывает внутри `db_session()`), одним запросом тянем `ProductHint` для всех `ozon_product_id` заявки.
- В `_product_key_and_meta` к Ozon-meta добавлены поля `hint_packaging` / `hint_notes`.
- В `_fill_sheet` (`:263-275`): для Ozon-листа в колонки `right_start` (Упаковка) и `right_start+1` (примечание) пишутся реальные значения, для WB — упаковка пока пустая (только Ozon в текущей итерации).

### Файлы
- `src/db/models.py` — модель ProductHint в конце.
- `alembic/versions/e799858ae69c_add_product_hints.py` — миграция.
- `src/parsers/product_hints.py` — новый файл.
- `src/services/product_hint_service.py` — новый файл.
- `src/bot/handlers/product_hints.py` — новый файл.
- `src/bot/states.py` — добавлен `ProductHints(StatesGroup)`.
- `src/bot/handlers/common.py` — кнопка в главном меню.
- `src/bot/main.py` — импорт + регистрация роутера.
- `src/generators/ship_tz.py` — загрузка hints + подстановка в колонки.

### Статус
Миграция накатана (`alembic upgrade head` → `e799858ae69c`). Бот рестартован, поллит. Импорт-смоук OK. Реальный xlsx ещё не лили — ждём end-to-end проверки.

### Будущее (открыто, не катать сейчас)
- WB: симметричная фича. Сейчас колонки «упаковка» / «состав набора» на WB-листе пустые.
- Скачать шаблон со всеми артикулами Ozon (чтобы юзер не вспоминал какие у него артикулы).
- Редактирование одного товара inline-кнопками через `/sku_list`.

---

## 2026-05-14 (13:30) — Time-picker, auto-poll на scoring-fail, auto-book mode, фиксы state-orphan

### Auto-poll теперь работает на scoring=FAILED (NO_TIMESLOTS / INVALID_ROUTE)
Раньше после `_show_all_failed_scoring_summary` бот просто сдавался — auto-poll не стартовал, потому что считал «это policy от Ozon, поллить бесполезно». Юзер: «почему тут не ставится на час поиск слотов».
- `failed_scoring` теперь хранит `cluster_id` / `supply_type` / `drop_off_warehouse_id` (раньше только `cluster + reason`) — auto-poll'у нужно для пересоздания.
- В `_show_all_failed_scoring_summary` стартуем `_auto_poll_slots` с `draft_id=0` и `created_ts=time-30мин` → первая итерация форсирует `_recreate_draft_for_auto_poll` → новые drafts → попытка timeslot/info.
- 404 «scoring not found» теперь смотрит на возраст draft'а: < 120с → ретрай в следующей итерации (scoring ещё дозревает), > 120с → выход. Раньше любой 404 убивал поллинг.
- После пересоздания добавил 30-сек паузу in-iter — даём scoring'у время посчитаться, иначе сразу 404.

### Time-picker — выбор часов отгрузки
Новый шаг wizard'а после dates picker. Семантика по требованию юзера:
- **«🎲 Любое время»** — без часового фильтра. Юзер тапает слоты руками (overview по датам → picker).
- **Выбрал конкретные часы** — это режим **auto-book**: бот сам берёт самый ранний слот в окне для каждого кластера и сразу bulk-book. Без слот-пикера.

Технически:
- Новое поле БД: `ShipmentRequest.target_hours_json` (список int 0..23). Миграция `390d74561795`.
- `kb_hours_picker` в `src/bot/keyboards.py`: 24 кнопки `HH–HH` (4 в ряд), multi-select с `✓`. Кнопки «✅ Дальше (N ч)» / «🎲 Любое время» / «✖ Отмена».
- Новый state `ShipPlan.hours` + handlers (`hp:N`, `hp_ok`, `hp_any`) в `src/bot/handlers/shipment.py`.
- `dp_ok` (подтверждение дат) теперь не сохраняет сразу, а ведёт на `ShipPlan.hours` с предзаполнением прошлого выбора часов.
- `_finalize_plan_with_hours` сохраняет даты + часы в БД и показывает карточку.
- В карточке заявки рядом с «Целевые даты» теперь «Часы: 09–12, 14–16» или «🎲 любое время».
- `format_picked_hours` в `helpers.py` — группирует подряд идущие часы в окна.

Фильтр применяется в:
- `_fetch_slots_for_drafts` (основной flow) — слоты с часом старта не в окне отбрасываются.
- `_run_autowalk` (Auto-walk, обычный и auto-mode).
- `_auto_poll_slots` (фоновый поиск, в том числе для scoring-fail кейса) — добавлен параметр `hour_picks`.

Auto-book реализация: `_fetch_slots_for_drafts` после фильтрации слотов проверяет `hour_picks` — если непустой, собирает `auto_choices` (первый слот на кластер) и зовёт `_run_bulk_book` напрямую. Picker не показывается, overview не открывается.

### Bulk-book вынесен в reusable `_run_bulk_book(bot, msg, state)`
`cb_ob_picker_confirm` стал тонкой обёрткой. Зовётся также из auto-book режима (часы выбраны).

### KeyError 'ob_rid' + «Drop-off-точки выбраны для 0 кластеров» + стущий wizard-лок
Состояние теряется (например после /start) → orphan callback с пустым state → раньше падал KeyError или мусорил «Drop-off для 0 кластеров». Чиню:
- `_create_drafts_and_fetch_scoring`: `data.get("ob_rid")` вместо `data["ob_rid"]`, при None — лог + сообщение «состояние потеряно».
- `_ask_dropoff_for_next_cluster`: при пустых rid/clusters — молча return.
- `_release_ozon_locks()` в `common.py` — вызывается на `/start` и `/cancel`, чистит `_WIZARD_IN_FLIGHT` и `_DRAFTS_CREATING`. Раньше стущий 30-мин лок не разлочить кроме как ждать TTL.

### Date overview — честный счётчик и нормальные квадраты
Юзер: «галки криво, там одна дата занята со временем, а у тебя все». И «странный белый прямоугольник заменить на нормальный».
- `_render_date_overview` теперь считает `total = len(data["ob_clusters"])` (все направления заявки), а не `len(clusters_with_slots)`. На фейловых кластерах строка «2/5» вместо вранья «2/2».
- Бар `🟩🟩⬜⬜⬜` (эмодзи) вместо `████░░░░` (text-shading) — на тёмной теме читается нормально.
- Кнопка «🚀 Все на DD.MM (M/N)» использует тот же honest total.
- Failed-scoring кластеры мерджятся с failed-timeslot в `ob_failed_clusters` для отображения в «⚠ Без слотов».

### Прочие UX-фиксы
- Слот-кнопки `_post_found_slots` БЕЗ `✓` per-кнопка (раньше все слоты на одну дату получали ✓ → казалось «всё занято»). Подсказка-сводка наверху сообщения: «💡 Уже забронировано: Самара 20.05 10:00 …».
- `cb_ob_found_slot_pick`: при тапе слота, сообщение сразу edit'ится в «⏳ Бронирую слот …» БЕЗ кнопок (анти-double-click + visual feedback). После _do_book_slot — финальный edit «✅ Слот забронирован …» с кнопками «🚀 Бронировать следующее направление (N осталось)» / «📋 К карточке».
- `_do_book_slot` теперь возвращает dict `{status, order_id, error}` чтобы caller мог нарисовать финал.
- Счётчик «N осталось» считает уникальные **кластеры** (а не items как раньше — выдавало «3 осталось» когда реально 1).
- `ozon_book_auto:rid:mode` — отдельный callback для «Бронировать следующее»: ставит `ob_auto_walk=True` → `_show_scored_warehouse_picker` для DIRECT сразу зовёт `_run_autowalk` без показа picker'а. Verbose info card в auto-режиме компактится в одну строку.
- DIRECT scoring picker: убрана мёртвая кнопка «ℹ Недоступно: N (скрыто)», текст в курсиве в теле сообщения.
- DIRECT → **Прямая** везде в UI: `«DIRECT 🚀»` → `«Прямая 🚀»`, `«Кроссдок»` → `«Кросс-докинг»`.
- Реальные выбранные даты вместо диапазона: `[20, 23]` → «20.05, 23.05» (а не «20.05–23.05» как раньше).
- Календарь дат: убраны «🧹 Сброс» и «✍ Вручную» кнопки.
- Команда «/ship_plan» в сообщении-ошибке заменена на инлайн-кнопку «📅 Запланировать даты».

### Status
Локально готово, синтакс + import-smoke OK. Не закоммичено в этой сессии — текущий блок планируется одним коммитом. Не пушено.

---

## 2026-05-14 (12:00) — UX-чистка: убрали лишние кнопки и команды-в-сообщениях

По итогам тест-прогона юзера (скрины в `data/screenshots/2026-05-14_08-52-05_*` и `08-53-47_*`, `08-49-39_*`):

### Чистка лишних кнопок
- `kb_dates_picker` (src/bot/keyboards.py:178): убрали «🧹 Сброс» и «✍ Вручную». Юзер: «лишние кнопки вручную и сброс убери». Раскладка теперь: даты 2-в-ряд → `[✅ Подтвердить (N)]` (full-width) → `[⏭ Без даты | ✖ Отмена]`.
- Хэндлеры мёртвого callback'а: убрали `cb_sp_clear`, `cb_sp_manual` (shipment.py), `cb_dp_clear`, `cb_dp_manual`, `cb_dp_back_to_calendar` (supply.py). Текстовый-парсер `supply_new_slot_date` оставил (escape hatch).
- В DIRECT scoring picker'е (`_show_scored_warehouse_picker`): убрали мёртвую кнопку «ℹ Недоступно: N (скрыто)» — она вела в `obscored_noop` → alert. Сведения о скрытых перенёс мелким курсивом в тело сообщения. Хэндлер `cb_ob_scored_noop` выпилил.

### Командные текстовки → инлайн-кнопки
- `_start_ozon_book_wizard` при отсутствии target_date_from раньше показывал «⚠ У заявки #N нет целевых дат. Сначала /ship_plan.» Юзер видел подсказку команды (`/ship_plan` без аргумента → «Использование: /ship_plan ID»). Теперь инлайн-кнопки «📅 Запланировать даты» (→ `ship_plan:rid`) + «◀ К карточке заявки». Командного синтаксиса юзер больше не видит.

### Терминология (план, ещё не реализовано)
По скрину Ozon ЛК (`2026-05-14_08-49-39_*`) согласовали маппинг: «Поставка #N: Москва, МО и Дальние регионы» = наше «направление», «Прямая»/«Кросс-докинг» = наши DIRECT/CROSSDOCK, «Склады размещения» = scored склад, «Точка отгрузки» = drop-off хаб. Применить поэтапно в карточке заявки + wizard.

### Даты per-cluster (план, ещё не реализовано)
Юзер согласовал концепт: на ship_plan-шаге выбор «🎯 Общие даты» (default, текущий флоу) или «🎯 Своя дата на направление» (последовательно спрашивает даты для каждого кластера до создания drafts). Под капотом: новый state-флаг `req.dates_mode` + per-cluster dates JSON. Подождёт следующей итерации.

---

## 2026-05-14 (11:45) — Single-flight на draft-creation + красивый финал drop-off picker'а

### Симптом (тест Щербинка→{Самара,Саратов,Тюмень,Уфа,Ярославль} 20-24.05)
Юзер тыкал «вперёд/назад» в пагинации хабов / «◀ К выбору» уже после того как drop-off на все 5 кластеров был выбран. Каждый клик стартовал **параллельный** `_create_drafts_and_fetch_scoring`. Лог показал 3+ перекрывающихся flow'а с пометками `♻ Переиспользую draft` (где cache попадал) + новые `POST /v1/draft/crossdock/create` (где не попадал). На последнем кластере (Ярославль) Ozon выкинул `429 на /v2/draft/create/info` (rate-limit 2 req/sec, оба потока долбили). Эпизод дубль-логов «✅ Drop-off-точки выбраны» 3 раза в одной сардельке.

### Фикс — `src/bot/handlers/ozon_book.py`
- **Новый второй уровень single-flight**: `_DRAFTS_CREATING: Dict[rid, ts]` + `_drafts_creating_acquire/release` (TTL 10 мин). Wizard-lock (`_WIZARD_IN_FLIGHT`) защищал только entry через карточку заявки — он не ловил повторные входы из drop-off callbacks. Новый лок взводится в **обёртке** `_create_drafts_and_fetch_scoring`, основное тело вынесено в `_inner` — чтобы не переписывать early-return'ы.
- **Превентивная проверка в `_ask_dropoff_for_next_cluster`**: при `idx>=len(clusters)` сначала смотрим `rid in _DRAFTS_CREATING or data.get("ob_drafts")`. Если уже идёт/завершено — `return` без записи в сардельку (раньше каждый клик «◀ К выбору» добавлял дубль строки «✅ Drop-off-точки выбраны:» даже если concurrent flow глушился).
- **Финал drop-off picker'а — edit без кнопок** (по запросу юзера: «прикольно же»). Когда последний кластер выбран, picker-сообщение через `msg.edit_text` замещается на «✅ Точки отгрузки выбраны: • <hub> → «<cluster>» …» с `reply_markup=None`. Фавориты / пагинация хабов / «◀ К выбору» больше не кликабельны → параллельный поток **физически невозможен** (а не только заглушен lock'ом). В сардельку — одна короткая строка «✅ Drop-off-точки выбраны для N кластеров.» (детали уже в picker'е).

### Что осталось проверить вживую
Pытнули бы заявку #37 ещё раз с тем же сценарием (Щербинка → 5 регионов). Если флоу один — фикс работает. Если опять дублы — копать дальше (`cb_obdo_back` / `cb_obdo_input` могут давать побочные пути).

---

## 2026-05-14 (11:30) — Багфикс CROSSDOCK: scoring=FAILED не должен идти в timeslot + меньше дублей сообщений

### Симптом (Самара/Саратов/Тюмень/Уфа/Ярославль, drop-off ДОМОДЕДОВО)
- 5 кластеров получили `status=FAILED` от scoring'а с `DROP_OFF_POINT_HAS_NO_TIMESLOTS` (Ozon-логистика не возит из ДОМОДЕДОВО в эти кластеры на 20-24.05).
- БОТ всё равно объявил «✅ Scoring готов. Для CROSSDOCK РФЦ определяет Ozon — иду к таймслотам.» **(ложь — на 5 дохлых драфтах)**.
- `_fetch_slots_for_drafts` прошёл по всем 5 → ловил `"can't find any calculation tasks"` → «🚫 у drop-off-точки нет таймслотов…» по 5 раз.
- Хуже: запустил `_auto_poll_slots` фоном → через ~60 сек тот же набор пошёл по второму кругу. Юзер видел дубль 5 + 4 строк «📅 Таймслоты draft …».
- Финальное сообщение «⚠ Не удалось получить слоты» давало генерик-причины «429 / 404 not in scoring / Слотов нет», хотя реальная причина уже была известна из scoring.

### Фикс — `src/bot/handlers/ozon_book.py`
- `_fetch_scoring_persistent` → сигнатура `(wh_list, fail_reason)`. `fail_reason` — короткий код фатального отказа Ozon (`NO_TIMESLOTS` / `INVALID_ROUTE` / `OUT_OF_ASSORTMENT` / `OTHER`). При cooldown/timeout/успехе — None (транзиентка, ретрайнем).
- Cached + fresh-create call-sites: dohly drafts с `fail_reason` **не попадают в `drafts_made`** → `_fetch_slots_for_drafts` их вообще не трогает → auto-poll не стартует.
- `drafts_made.append` переставлен ПОСЛЕ scoring-проверки (раньше добавлялся ДО → дохлые драфты тянулись дальше по pipeline-у).
- Удалён мёртвый wrapper `_fetch_scoring_persistent_with_state` (нигде не звался).
- Новый `_show_all_failed_scoring_summary` (~ozon_book.py:920): если ВСЕ кластеры отбиты scoring'ом, показываем сгруппированный итог с правильными причинами + одной кнопкой «◀ К карточке заявки». Auto-poll НЕ запускаем — бесполезно при `NO_TIMESLOTS`/`INVALID_ROUTE`, это политика Ozon, не rate-limit.
- State: добавил `ob_failed_clusters_scoring=[…]` — для будущего отображения failed-кластеров в overview-экране (mixed-success/fail case).

### Параллельно — фикс «3 сообщения после загрузки PDF» (returns flow)
- `src/bot/handlers/returns.py:cb_ret_ozon`: PDF теперь без `_back_kb`. Под ним отдельное маленькое текст-сообщение `«PDF готов — скачай и приложи на ПВЗ.»` с кнопками. Раньше клавиатура висела на document-message → при back-нав `safe_edit_or_answer` падал на `edit_text` (документ нельзя превратить в текст), фолбэк в `answer()` создавал новое сообщение. К концу флоу копились 3+ сообщения.
- `send_long` → `safe_edit_or_answer` в `cb_ret_ozon`/`cb_ret_wb`: тексты <3900 симв, send_long всегда answer() → плодит дубли.

### Параллельно — `_send_attach_result` слился с заголовком (shipment.py)
- Раньше 3 callback-handler'а (`cb_ship_new`, `cb_ship_attach`, `cb_up_otype`) делали `safe_edit_or_answer(cb.message, "✅ Создана/Привязана…")` а потом `_send_attach_result(cb.message, …)` через `send_long` — 2 сообщения подряд.
- Теперь `_send_attach_result(..., header="✅ …")` — один edit-call. Send_long → safe_edit_or_answer (контент <1000 симв).

### Параллельно — «✖ Авто-поиск остановлен» теперь edit а не answer
- `cb_ob_cancel_poll`: было `cb.message.answer(...)` → плодил хвост к «⚠ Не удалось получить слоты». Теперь edit'ом в место → одно сообщение с кнопкой «◀ К карточке заявки».

### Статус
- Синтакс + import-smoke OK. Не тестировался end-to-end на боте — нужна реальная FAILED-сцена (ДОМОДЕДОВО + регионы где нет таймслотов).
- Тесты pytest не гонял.

---

## 2026-05-14 (11:00) — Обзорный экран дат + auto-poll до 1 часа + смена селлер-ключа

### .env
Подменили основной Ozon-кабинет: `APIKEY_OZON=50c1cf93-…` / `CLIEN_TID=2154472`. Прошлые ключи сохранены закомментированными для отката.

### Обзорный экран после scoring
После того как timeslot/info собрал слоты по всем drafts, **больше НЕ открываем сразу slot-picker**. Сначала показываем «📅 Сводка по датам»:
- Прогресс-бар по каждой дате: `18.05 ████████ 5/5 — Самара, Саратов, Тюмень, Уфа, Ярославль`
- Кластеры без слотов (drop-off не возит / пустой ответ) отдельной строкой с подсказкой сменить хаб.
- Кнопки: «🚀 Все на DD.MM (M/N)» × до 6 самых перспективных дат, «🎯 Выбрать слоты вручную», «✖ Отмена».
- Callback `obauto:<date>` — для каждого кластера берёт самый ранний слот в этот день, пропускает кластеры без слотов в этот день, идёт сразу к confirm-panel.
- Callback `obmanual` — текущий per-cluster picker.

Реализация: `_render_date_overview()` в `src/bot/handlers/ozon_book.py:1981`. Triggered из `_fetch_slots_for_drafts` вместо прямого вызова picker'а. Новые поля state: `ob_overview_msg_id`, `ob_failed_clusters`.

### Auto-poll прокачан до 60 мин + пересоздание draft
Юзер: «долбить час, если не найдёт — сорри братан, и не мешалось с другими поставками».
- TTL: `25 → 60` мин.
- `_recreate_draft_for_auto_poll()` — если draft старше 28 мин, пересоздаём через `oz.draft_create` (items из БД, cluster_id/drop_off/type из обогащённого failed_drafts dict). На неудачу пересоздания auto-poll выживает (долбит старый, пока тот не expired).
- failed_drafts перед запуском auto-poll обогащаются: `drop_off_warehouse_id` (из `ob_dropoff_choices`), `draft_type`, `created_ts`.
- Финальное сообщение братан-стайл: «😔 Извини, братан — за час Ozon так и не отдал слотов…».
- «Слотов нет на даты» — раньше сразу выход, теперь продолжаем долбить, раз в 5 мин шлём status.
- Изоляция уже была (ключ `_AUTO_POLL_TASKS[rid]`), подтверждено.

### Статус
Перезапустил бот (PID 13800). Жду тест:
1. Заявка с 5 кластерами → должен показаться обзорный экран с прогресс-барами.
2. Тап «🚀 Все на 18.05» → confirm-panel → bulk-book.
3. Или «🎯 Выбрать вручную» → текущий picker.

### Не сделано (отдельно)
- Кнопка «🔁 Сменить drop-off для кластера X» прямо из обзора (для failed-кластеров) — task #7 pending. Пока юзер должен возвращаться в карточку заявки и запускать CROSSDOCK-мастер заново; failed-кластеры подхватятся.

---

## 2026-05-14 (10:00) — UX-блок: статусы по кластерам, продолжение, slot-picker, источники

5 фиксов одним заходом.

### 1. Статус заявки — только когда все кластеры booked
`req.state = "supplies_created"` ставился после ЛЮБОЙ успешной брони → заявка пропадала из активного списка даже когда забронированы 1-2 из 5. Добавлен `refresh_request_state_after_booking(req)` в `src/services/shipment_service.py:155`: ставит «supplies_created» только если ВСЕ `items.booked_supply_id` заполнены. Вызывается из обоих мест где раньше был хардкод (`_book_one_slot`, `_do_book_slot`).

### 2. Кнопка «🔁 Продолжить с оставшимися (N)» в финале bulk-book
Если `fail_count > 0` после bulk-book, в финальной клавиатуре первая кнопка — продолжить wizard для незабронированных кластеров. Mode (`cross`/`direct`) определяется из `ob_type` в state перед `state.clear()`. Колбек переиспользует существующий `ozon_book_card:<rid>:<mode>`, который уже корректно отфильтровывает booked-кластеры по `booked_supply_id`.

### 3. Slot picker: 15 кнопок на страницу + убрать «#0» для CROSSDOCK
`SLOTS_PER_PAGE = 8` → `15`. Новый хелпер `_is_crossdock_wh(name, wh_id)` детектит CROSSDOCK-слоты (warehouse_id=0 / пустое имя / «#0»). На кнопках слота и в confirm-summary при CROSSDOCK больше НЕ показываем «→ #0» — там только дата-время. РФЦ всё равно определит Ozon на этапе supply/create.

### 4. Карточка заявки: ✅ / 🟡 / ⏳ по каждому кластеру
`shipment_summary` (`src/services/shipment_service.py:172`) теперь считает по кластеру: сколько items забронированы. Маркер `✅` (всё), `🟡` (частично), `⏳` (ни одного). Для booked-кластеров под строкой показывает `order_id`, склад и время слота из первого booked-item.

### 5. Убрана секция «Источники: • file1.xlsx …» из карточки заявки
`_render_request_card` в `src/bot/handlers/shipment.py:183` — блок удалён. Пользователь сказал не нужен.

### Статус
Перезапустил бот (PID 15836). Все 5 фиксов задеплоены. Ждём проверку на новой заявке.

---

## 2026-05-14 (09:30) — Anti-double-click для «🚀 Создать поставку Ozon»

### Что
Заявка #31: пользователь нетерпеливо тапнул кнопку «🚀 Создать поставку Ozon» три раза подряд → три параллельных wizard'а начали создавать drafts/тянуть scoring для одних и тех же кластеров. Результат — каша из перекрывающихся сарделек, ошибки «Multi cluster draft NNN doesn't exist» (один поток инвалидировал draft другого), 429-лавина.

### Как
- `src/bot/handlers/ozon_book.py`:
  - Новый module-level lock `_WIZARD_IN_FLIGHT: Dict[int, float]` (rid → start_ts) + хелперы `_wizard_acquire/_wizard_release`. TTL 30 минут — если wizard завис, lock авто-сбросится.
  - `cb_ozon_book_from_card`: на повторный клик отвечает alert'ом «Ozon-мастер уже запущен», НЕ запускает второй поток.
  - Сразу после успешного acquire — `edit_reply_markup(reply_markup=None)` на исходной карточке заявки, чтобы кнопки больше нельзя было ткнуть.
  - `_start_ozon_book_wizard` теперь возвращает `bool` (взлетел / early-exit). Если early-exit (нет ключей/нет дат/всё забронировано) — caller сразу release'ит lock.
  - `_release_wizard_for_state(state)` — release по rid из FSM state. Навешен в exit-points: SKU-блокировка (1327), «ни один draft не создан» (1409), «все кластеры выбраны» (906).
  - `cb_ob_picker_cancel` + `cb_ob_picker_confirm` — release в финале.

### Почему
Раньше единственная защита от двойной брони — `_BOOKING_IN_FLIGHT` по `draft_id`, но она срабатывала ПОЗЖЕ, когда драфты уже созданы. До этого момента параллельные wizard'ы успевали наделать дублей. Lock на уровне `request_id` отсекает второй wizard в самом начале + гасит кнопки чтобы не было соблазна тапать.

### Статус
Сделано. Бот перезапущен (PID 25568). Жду тест на свежей заявке — двойной тап должен дать alert, не запустить параллель.

---

## 2026-05-14 (09:10) — Bulk-book: жёсткая пауза 60с между кластерами

### Что
Заявка #30: при bulk-бронировании 5 кластеров CROSSDOCK первые 2 (Самара, Саратов) брались успешно, а Тюмень/Уфа/Ярославль падали с `Ozon 429 на /v2/draft/supply/create: request rate limit per second`. Внутренние ретраи (1× через 30с в `_request` + 2× по 45с в `_book_one_slot`) не помогали — окно per-second не успевало разжаться, потому что между кластерами в bulk-loop пауз не было вовсе.

### Как
- `cb_ob_picker_confirm` (`src/bot/handlers/ozon_book.py:2188`): добавлена жёсткая пауза перед каждым кластером кроме первого. Базово 60с, после кластера с 429 — 90с. В сардельку пишется «⏱ Пауза 60с перед следующим…» чтоб бот не выглядел зависшим.
- `_book_one_slot` теперь возвращает `Tuple[bool, bool]` — `(ok, was_429)`. `was_429=True` если на `supply/create` сработал хоть один 429 (даже если потом ретрай прошёл). Bulk-loop использует флаг для адаптивной паузы.
- Внутренний retry в `_book_one_slot` поднят с 45с до 60с — выровнен с per-second окном Ozon (пользователь подтвердил: бот ЛК ждёт ~60-61с).

### Почему
Per-second лимит у Ozon — скользящее окно, и три кластера подряд после короткой паузы продолжают долбить тот же endpoint. 60с между вызовами стабильно сбрасывает окно. Запас 90с после 429 — на случай если предыдущий кластер уже «прогрел» бан.

### Статус
Сделано. Перезапустил бота. Ждём следующий bulk-тест.

---

## 2026-05-14 — Unified slot picker (1 панель → 5 выборов → bulk-book)

### Что
Заменили старый «один большой список из всех слотов всех кластеров → тап → тут же бронь → кнопка „Забронировать остальные“» на единую edit-message панель с пошаговым выбором по кластерам.

### Как
- `_fetch_slots_for_drafts` (`src/bot/handlers/ozon_book.py`): больше не строит общий `all_buttons` и не пишет `slot_N` в FSM. Вместо этого собирает `clusters_with_slots = [{cluster, draft_id, cluster_id, supply_type, drop_off_name, slots:[...]}]` и вызывает `_render_picker_panel`.
- Новые функции:
  - `_render_picker_panel(msg, state)` — edit_text одной панели. Шапка с прогрессом по всем кластерам (`✅ выбрано / 🟡 выбираю… / ⏳ ожидает`), список слотов текущего кластера (8 на страницу), кнопки `◀ Назад / Вперёд ▶`, `↩ Перевыбрать «<кластер>»`, `✖ Отмена`.
  - `_render_confirm_panel(msg, state)` — финальный экран со сводкой выборов + «🚀 Забронировать всё».
  - `_book_one_slot(bot, msg, state, slot, rid)` — supply/create + polling status + запись в БД; пишет всё в общую сардельку через `progress_add`.
- Callbacks (все на `OzonBook.pick_slot`):
  - `obps:<idx>:<sn>` — pick слота, idx+=1, re-render.
  - `obpg:<idx>:<page>` — пагинация.
  - `obpback:<idx>` — сброс выбора с target_idx и далее, перевыбор.
  - `obpconfirm` — bulk-book последовательно.
  - `obpcancel` — отмена.
- `_do_book_slot`: убран блок «Подсказка: 🚀 Забронировать остальные (N)» — больше не нужно, picker делает всё в одном проходе. Эта функция теперь работает только в auto-poll (одиночный слот после 429).
- Старый `cb_ob_slot_pick` (на `obslot:`) удалён вместе с этим callback'ом.

### Почему
Раньше каждый тап слота сразу бронил → юзер ловил отдельное сообщение → жал «Забронировать остальные» → бот сначала спрашивал drop-off, потом scoring, потом снова кучу слотов. Много сообщений, контекст теряется. Теперь:
1. Все слоты собираются разово.
2. Юзер видит ОДНУ редактируемую панель, листает.
3. Тапает первый, видит «✅ Москва — 17.05 10:00», edit_text открывает Тюмень.
4. И так до последнего, в конце — сводка → одна кнопка «🚀 Забронировать всё» → bulk-book последовательно.

### Статус
Работает (нужно протестировать на живой заявке). Перезапустил бот.

---

## 2026-05-13 (13:13) — CROSSDOCK end-to-end + UX-сардельки + точки кроссдока

### CROSSDOCK финализирован
- `OzonClient.draft_create` — убран хардкод `drop_off_warehouse=ДОМОДЕДОВО_РФЦ_КРОССДОКИНГ`. Теперь требует параметр `drop_off_point_warehouse_id` от вызывающего; для CROSSDOCK кидает ошибку если не передан.
- `_fetch_scoring_persistent`: для `status=SUCCESS` НЕ ретраит даже когда `wh_list` пустой. У Ozon при CROSSDOCK scoring возвращает `storage_warehouse=null` (РФЦ назначения Ozon выбирает сам), это нормальное поведение.
- `_show_scored_warehouse_picker`: при CROSSDOCK пропускает шаг выбора склада, сразу идёт к `_fetch_slots_for_drafts`. Логика «выбери конкретный РФЦ» только для DIRECT.
- Также фикс: переменная `state` в parser scoring перекрывала параметр функции — переименована в `wh_state`.

### Точки кроссдока (фаза 2 + 3)
- Новые таблицы `favorite_crossdock_points` (id, name, warehouse_id, point_type, use_count, last_used_at) и `ozon_drafts` (cache draft_id для переиспользования <25 мин).
- Меню → «⭐ Точки кроссдока»: список любимых, добавление через имя/ID с поиском, удаление.
- Поиск через `/v1/warehouse/fbo/list` filter=CROSSDOCK + fallback на `/v1/cluster/list`. FBS endpoint не используем (по требованию пользователя).
- UI: типы переведены на русский (РФЦ/СЦ/Кроссдок/Точка сдачи/ПВЗ), маркер «(Рекомендуется)» только для крупных. Сортировка: крупные → ПВЗ.
- Pagination 8 на страницу с «◀ Назад / Вперёд ▶».
- Common-prefix stripping срезает только по границам слов (`_`, ` `, `,`, `.`, `-`, `/`) и только при ≥4 результатах на странице.
- Module-кэш `_RECENT_MATCHES` как safety belt если FSM state потеряется.
- В CROSSDOCK-флоу: до создания draft бот спрашивает drop-off-точку для каждого кластера. UI: ⭐ Любимые сразу + 🏭 «Все хабы» (пагинированный список 71 CROSS_DOCK из cluster_list) + ✏ «Ввести имя» (поиск как в favorites).

### Drafts persistence
- Таблица `ozon_drafts(request_id, cluster, cluster_id, draft_id, supply_type, drop_off_warehouse_id, created_at, used_at)`.
- `src/services/draft_cache.py`: `get_fresh_draft`, `save_draft`, `mark_draft_used`, `cleanup_expired`. TTL 25 мин (Ozon держит draft 30 мин, оставляем запас).
- При повторном «🚀 Создать поставку Ozon» бот переиспользует свежие drafts без `POST /draft/*/create` — экономит 15 сек на каждый кластер + не палит лимит 2/мин.

### Pre-check SKU
- Перед `draft_create` бот тянет каталог текущего Ozon-кабинета (`product_list`+`product_info_list`) и сверяет наши `ozon_sku` со списком. Если есть «чужие» SKU (из другого кабинета) — блокирует с явным сообщением и инструкцией пересинхронизировать через `/sku_link_ozon`.

### UX: одна «сарделька» вместо россыпи сообщений
- `src/bot/helpers.py`: `progress_start` / `progress_add` / `progress_reset` — единый status-message, обновляется через `edit_text` по мере прогресса. При переполнении 3800 символов — стартует новое.
- Замены `msg.answer(...)` на `progress_add(...)` в `_create_drafts_and_fetch_scoring`, `_fetch_scoring_persistent`, `_show_scored_warehouse_picker`, `_fetch_slots_for_drafts`, `_do_book_slot`.
- Сообщения с кнопками (drop-off picker, slot picker, final summary) остаются отдельно — Telegram inline keyboard на edit'нутом сообщении сложно менять.

### Возвраты
- Ozon: добавлен `/v1/returns/list` (universal FBO+FBS). Фильтр на сторону бота: показываем только actionable (visual.status «В пункте выдачи» / `ArrivedAtReturnPlace`), архив скрываем. PDF этикетки получения отдаётся одним сообщением + caption со списком SKU.
- WB: `GET /api/v1/supplier/sales?dateFrom=30d` → фильтр по `saleID` начинающимся с `R` (рефанды). Внятная подсказка что возвраты «в пути» в seller-API не доступны (только в `wildberries.ru/lk/myorders/delivery`).
- Ключевое: Ozon `get-pdf` возвращает JSON с base64 в поле `pdf` (не `file_content` как в офиц. доке) — поддерживаем оба ключа.

### Скриншоты от пользователя
- `handle_photo` в upload.py сохраняет фото в `data/screenshots/{ts}_{uniq}.jpg` и отвечает путём. Claude читает напрямую через Read tool.

### Структурные правки
- 🛒 «Состав по кластерам» в карточке заявки — показывает все SKU по группам, отметка `✓ → склад · #order_id` для забронированных.
- `_state_label`: русские подписи статусов в UI. `planning` теперь показывается как «✏ Черновик» (как и `draft`) — пользователь так лучше воспринимает.
- Авто-прогресс к следующему кластеру после успешной брони (был latent bug что rid не передавался в `_do_book_slot` через auto-walk path).
- Skip уже забронированных кластеров при повторном входе в Ozon-флоу.
- Auto-walk: фиксил отсутствие `cluster_id`/`supply_type` в slot dict (нужны для v2 API).

### Memory обновлён
- `reference_ozon_fbo_api.md`: раздел про возвраты + `get-pdf` отдаёт `pdf` key.
- `feedback_dev_principles.md` (новый): «без костылей, MVP-ready, лишнее стирать сразу».

### Статус
End-to-end CROSSDOCK работает: создан реальный order_id=104267181 на Ростов через МО_ЩЕРБИНКА_ХАБ.

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
