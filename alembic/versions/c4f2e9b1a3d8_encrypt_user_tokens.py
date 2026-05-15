"""encrypt existing user tokens (Fernet) — идемпотентная миграция данных

Revision ID: c4f2e9b1a3d8
Revises: b8d2e7a91f02
Create Date: 2026-05-15 13:55:00.000000

Если в users.ozon_api_key / wb_api_key лежит plain-токен — шифруем.
Если уже зашифровано (детект по префиксу gAAAAAB) — пропускаем.
Если TOKEN_ENCRYPTION_KEY не задан в env — вообще ничего не делаем
(оставляем plain, миграция no-op). На старте бота будет WARN.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.sql import text


revision: str = 'c4f2e9b1a3d8'
down_revision: Union[str, None] = 'b8d2e7a91f02'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Импорт внутри функции — в момент чтения модуля при autogenerate
    # cryptography может быть не доступен.
    import os
    key = os.getenv("TOKEN_ENCRYPTION_KEY", "").strip()
    if not key:
        # Без ключа миграция нечего делать — данные останутся plain.
        # Это OK для dev. На проде юзер должен поставить ключ ДО первого upgrade.
        return
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        return
    f = Fernet(key.encode())
    bind = op.get_bind()
    rows = bind.execute(text("SELECT tg_id, ozon_api_key, wb_api_key FROM users")).fetchall()
    for row in rows:
        tg_id, ozon_key, wb_key = row[0], row[1], row[2]
        updates: dict = {}
        if ozon_key and not ozon_key.startswith("gAAAAAB"):
            updates["ozon_api_key"] = f.encrypt(ozon_key.encode()).decode()
        if wb_key and not wb_key.startswith("gAAAAAB"):
            updates["wb_api_key"] = f.encrypt(wb_key.encode()).decode()
        if updates:
            sets = ", ".join(f"{k} = :{k}" for k in updates)
            updates["tg_id"] = tg_id
            bind.execute(text(f"UPDATE users SET {sets} WHERE tg_id = :tg_id"), updates)


def downgrade() -> None:
    # Расшифровать обратно — если ключ есть. Иначе ничего.
    import os
    key = os.getenv("TOKEN_ENCRYPTION_KEY", "").strip()
    if not key:
        return
    try:
        from cryptography.fernet import Fernet, InvalidToken
    except ImportError:
        return
    f = Fernet(key.encode())
    bind = op.get_bind()
    rows = bind.execute(text("SELECT tg_id, ozon_api_key, wb_api_key FROM users")).fetchall()
    for row in rows:
        tg_id, ozon_key, wb_key = row[0], row[1], row[2]
        updates: dict = {}
        for col, val in (("ozon_api_key", ozon_key), ("wb_api_key", wb_key)):
            if val and val.startswith("gAAAAAB"):
                try:
                    updates[col] = f.decrypt(val.encode()).decode()
                except InvalidToken:
                    pass
        if updates:
            sets = ", ".join(f"{k} = :{k}" for k in updates)
            updates["tg_id"] = tg_id
            bind.execute(text(f"UPDATE users SET {sets} WHERE tg_id = :tg_id"), updates)
