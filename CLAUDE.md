# Postavka Assistant Bot

Telegram-бот для ИП Баковец × ЛЕБЕР: автоматизирует поставки на Wildberries (FBW) и Ozon (FBO/FBS). MVP — Ozon end-to-end через API; для WB пока только read (write-API закрыт, водитель/коробки заводятся в ЛК руками).

## ⚡ Bootstrap для новой сессии — читать первым

Когда начинаешь работать с проектом после рестарта Claude или новой сессии — **в таком порядке**:

1. **Auto-memory** уже подгружена в начало контекста — там профиль пользователя, конвенции, refs. Сверься с `MEMORY.md` индексом.
2. **`memory/project_current_focus.md`** — что было сделано на прошлой сессии, что открыто, чем продолжить. Это **live-state**, обновляется руками в конце каждой сессии.
3. **`WORKLOG.md` в корне репо** — 3-4 последние записи. Там подробности (файл:строки, почему именно так).
4. **`git status` + `git log -5 --oneline`** — что в рабочей копии, что закоммичено. Юзер коммитит сам по команде, не делать `git commit` без явной просьбы.
5. **`data/screenshots/`** — если юзер кидает скрин, он попадает сюда.

При длинной сессии — в конце обновить `project_current_focus.md` + дописать запись в `WORKLOG.md`. Это **главные две точки** чтобы следующая сессия не теряла контекст.

## Стек
Python 3.10 · aiogram 3 · SQLAlchemy 2 + SQLite (`data/bot.db`) · Alembic · openpyxl · xlrd 2.0.1 · anthropic SDK (LLM-fallback парсер) · httpx + httpx-socks (SOCKS5-прокси к Ozon API).

## Структура
- `src/bot/main.py` — точка входа, регистрация роутеров.
- `src/bot/handlers/` — команды/callbacks по доменам:
  - `shipment.py` — карточка заявки, /ships, /ship_plan.
  - `ozon_book.py` — Ozon FBO wizard (drop-off → scoring → slots → bulk-book), auto-poll, picker, обзорный экран дат.
  - `returns.py`, `integrations.py`, `favorites.py`, `upload.py`, `common.py`, `catalog.py`, `supply.py`, `export.py`.
- `src/integrations/ozon_api.py` — Ozon Seller API клиент v2 (с retry на 429 / cooldown / anti-abuse).
- `src/integrations/wb_api.py` — WB API клиент (read-only для FBW).
- `src/integrations/_cache.py` — файловый кэш (cluster_list, scoring) + persistent cooldown.
- `src/db/models.py` — модели: ShipmentRequest, ShipmentItem, OzonProduct, WbProduct, OzonDraftCache, FavoriteCrossdockPoint и т.п.
- `src/services/` — бизнес-логика (`shipment_service`, `draft_cache`, `catalog_service`, `reconciler`, `slot_hunter`).
- `src/generators/` — генерация ТЗ Приёмки/Отгрузки (docx через python-docx).
- `src/parsers/` — парсинг входящих xlsx (LLM-fallback на anthropic).
- `alembic/versions/` — миграции.

## Runbook (все команды из корня проекта)

| Что | Команда |
| --- | --- |
| Запуск бота | `python -m src.bot.main` |
| Тесты | `python -m pytest -v` |
| Применить миграции | `python -m alembic upgrade head` |
| Создать миграцию | `python -m alembic revision --autogenerate -m "<msg>"` |
| Пересидить каталог | `python -X utf8 scripts/seed_catalog.py` |

`.env` должен содержать: `TELEGRAM_BOT_TOKEN`, `ALLOWED_USER_ID`, `APIKEY_OZON`, `CLIEN_TID` (Ozon client_id), `APIKEY_WB`, `APIKEY_CLAUDE`, `OZON_PROXY_URL` (SOCKS5 — `rdns=True` обязателен в httpx-socks).

## Ключевые точки и инварианты

- **WORKLOG.md** в корне — журнал изменений по сессиям, дописывается после каждого осмысленного блока работы. Свежие записи сверху. Не затирать старое.
- **Ozon FBO API нюансы**: после 16.03.2026 актуальны v2-эндпоинты `/v2/draft/supply/create` и `/v2/draft/supply/create/status`. Timeslot в payload v2 — БЕЗ `Z`. См. также `ozon_api.txt` и memory `reference_ozon_fbo_api`.
- **Ozon draft TTL** — 30 минут. Кэш в `OzonDraftCache`, переиспользуем до 25 мин (запас 5 мин).
- **Anti-double-click**: в `ozon_book.py` есть `_WIZARD_IN_FLIGHT` (rid → start_ts), TTL 30 мин. Защищает от двойного клика «Создать поставку Ozon» (раньше параллельные wizard'ы создавали дубли drafts).
- **Rate limits Ozon**: `/v2/draft/supply/create` имеет per-second лимит. В bulk-book между кластерами **60с** базовая пауза, **90с** после 429. Внутренний retry 60с на cooldown.
- **CROSSDOCK**: товары на drop-off хаб, Ozon сам развозит. `warehouse_id` в slot = 0 (РФЦ определяет Ozon на этапе supply/create). Для DIRECT — `warehouse_id` = конкретный РФЦ.

## Конвенции работы (важно при правках)
- **Inline-buttons only** — все взаимодействия с пользователем через inline-keyboard, `edit_text` вместо новых сообщений. Не плодить сообщения, дёргать `progress_add` (одна «сарделька») для статусов.
- **Autonomous action** — правки и рестарты бота делаются без подтверждения пользователя. Спрашивать только перед опасными действиями (force-push, drop database, send messages в другие чаты).
- **Бот рестартуем сами после ЛЮБОГО изменения handler/keyboards/services/models** — не просим юзера. Перезапуск: найти `python.exe` через `Get-Process python`, `Stop-Process -Id <pid> -Force`, потом `python -m src.bot.main` в фоне. После старта проверить `Get-Process -Id <pid>` что не упал. Юзер хочет видеть рабочую правку сразу, не вспоминать «надо ребутнуть».
- **Без костылей, MVP-ready** — не плодить временные хаки, лишнее стирать сразу. Multi-tenant в перспективе — но сейчас single-tenant.
- **Коммиты — только по команде пользователя.** Не коммитить автоматически. История — через WORKLOG.md.
- **Russian responses** — пользователь не разработчик, общение и логи на русском.

## Внешние ссылки в репо
- `ozon_api_docs.txt` / `*.pdf` — официальная документация Ozon Seller API v2.1.
- `data/screenshots/` — скрины из чата (юзер кидает).
- WB / Ozon ЛК: `seller.ozon.ru/app/supply-orders`, `seller.wildberries.ru/supplies-management/all-supplies`.

## Память Claude
User-level auto-memory: `C:\Users\vladi\.claude\projects\C--Users-vladi-OneDrive-...-postavka_assistant_bot\memory\`. Там лежат фиксированные preferences (русский, inline-buttons, без live-log narration), профиль пользователя, ссылки на reference-файлы, milestone-точки. Перед действиями свериться с актуальностью.
