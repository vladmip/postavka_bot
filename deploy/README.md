# Деплой Postavka Assistant Bot на VPS

Минимальный VPS: 1 CPU, 1 ГБ RAM, 10 ГБ диска. Любая Linux с systemd (Ubuntu 22.04 LTS — рекомендуется).

## Шаги

```bash
# 1. Создать пользователя для бота (без shell-логина).
sudo useradd -r -s /usr/sbin/nologin -d /opt/postavka -m postavka

# 2. Поставить системные пакеты.
sudo apt update && sudo apt install -y python3.11 python3.11-venv sqlite3 git

# 3. Клонировать репо.
sudo -u postavka git clone https://github.com/vladmip/postavka_bot.git /opt/postavka

# 4. venv + deps.
sudo -u postavka python3.11 -m venv /opt/postavka/venv
sudo -u postavka /opt/postavka/venv/bin/pip install -e /opt/postavka

# 5. Сгенерировать Fernet master-key для шифрования токенов.
sudo -u postavka /opt/postavka/venv/bin/python -c \
  "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# → строку выше скопировать в .env как TOKEN_ENCRYPTION_KEY=...

# 6. Создать .env (см. .env.example в корне репо).
sudo -u postavka cp /opt/postavka/.env.example /opt/postavka/.env
sudo -u postavka nano /opt/postavka/.env
# Минимально нужно: TELEGRAM_BOT_TOKEN, ALLOWED_USER_ID (твой tg_id, для /admin_stats),
# TOKEN_ENCRYPTION_KEY. Остальное (APIKEY_OZON и т.п.) — опционально, как fallback
# для legacy single-tenant; новые юзеры вводят свои ключи через onboarding.

# 7. Накатить миграции БД.
sudo -u postavka /opt/postavka/venv/bin/python -m alembic -c /opt/postavka/alembic.ini upgrade head

# 8. systemd unit.
sudo cp /opt/postavka/deploy/postavka-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now postavka-bot
sudo systemctl status postavka-bot

# 9. Логи смотреть так:
sudo journalctl -u postavka-bot -f          # live
sudo tail -f /opt/postavka/logs/bot.log     # файл (ротация 5 МБ × 5)

# 10. Cron-бэкап БД.
sudo chmod +x /opt/postavka/deploy/backup.sh
echo "0 4 * * * /opt/postavka/deploy/backup.sh >> /var/log/postavka-backup.log 2>&1" \
  | sudo crontab -u postavka -
```

## Обновление

```bash
sudo -u postavka git -C /opt/postavka pull
sudo -u postavka /opt/postavka/venv/bin/pip install -e /opt/postavka
sudo -u postavka /opt/postavka/venv/bin/python -m alembic -c /opt/postavka/alembic.ini upgrade head
sudo systemctl restart postavka-bot
```

## Безопасность

- **TOKEN_ENCRYPTION_KEY не теряй и не коммить.** Если ключ потеряется — все токены юзеров в БД станут нечитаемы, юзерам придётся вводить заново.
- **`.env` владеет postavka:postavka, режим 600.** `chmod 600 /opt/postavka/.env`.
- **БД (`data/bot.db`) тоже не должна быть world-readable.** systemd-unit ограничивает доступ.
- **Прокси Ozon SOCKS5** в `OZON_PROXY_URL` — общий на всех юзеров. Если IP получит 429 от Ozon — импактует всех. На больших масштабах надо думать.
- **Telegram API rate-limit** — 30 msg/sec global. Между юзерами в digest scheduler стоит `sleep(2)`.
- **Rate limiter в боте** — 30 действий/минуту, 200 действий/час per юзер (in-memory). Перезапуск сбрасывает счётчики.

## Troubleshooting

| Симптом | Действие |
|---|---|
| Бот не стартует | `journalctl -u postavka-bot -n 50` — смотрим traceback |
| `TOKEN_ENCRYPTION_KEY невалиден` | Сгенерировать заново и положить в .env, юзерам ввести креды заново |
| Юзер не может ввести API key | Проверить логи на rate-limit; в БД `users` есть запись? |
| /digest не приходит утром | Проверить логи: `DIGEST scheduler: sleep ... МСК` — должно быть в логах |
| БД залочена (locked) | sqlite write-lock — не должно случаться (1 процесс), но если — restart bot |
