#!/usr/bin/env bash
# Daily backup of bot.db. Поставить в cron под юзером postavka:
#   sudo -u postavka crontab -e
#   0 4 * * * /opt/postavka-bot/deploy/backup.sh >> /opt/postavka-bot/logs/backup.log 2>&1
# Хранит бэкапы 14 дней.
set -euo pipefail
# cd чтобы не было `find: Failed to restore initial working directory: /root`,
# когда скрипт стартует не из домашней папки postavka.
cd "$(dirname "$0")/.."

DATA_DIR="${DATA_DIR:-/opt/postavka-bot/data}"
BACKUP_DIR="$DATA_DIR/backups"
KEEP_DAYS="${KEEP_DAYS:-14}"

mkdir -p "$BACKUP_DIR"

TS=$(date +%Y-%m-%d_%H%M%S)
SRC="$DATA_DIR/bot.db"
DST="$BACKUP_DIR/bot-$TS.db"

if [ ! -f "$SRC" ]; then
    echo "ERR: $SRC не существует" >&2
    exit 1
fi

# SQLite-safe бэкап через .backup команду — атомарный, переживает write'ы.
sqlite3 "$SRC" ".backup '$DST'"
echo "OK: $DST ($(du -h "$DST" | cut -f1))"

# Чистка старых
find "$BACKUP_DIR" -name "bot-*.db" -mtime "+$KEEP_DAYS" -delete -print
