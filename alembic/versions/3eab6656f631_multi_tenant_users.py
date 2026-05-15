"""multi-tenant: users table + user_id FK + migrate existing rows to ALLOWED_USER_ID

Revision ID: 3eab6656f631
Revises: 1e9a33933f2c
Create Date: 2026-05-15 09:00:00.000000

"""
import os
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.sql import text


revision: str = '3eab6656f631'
down_revision: Union[str, None] = '1e9a33933f2c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()

    # 1. users.
    op.create_table(
        'users',
        sa.Column('tg_id', sa.Integer(), primary_key=True),
        sa.Column('supplier_name', sa.String(length=128), nullable=True),
        sa.Column('ozon_client_id', sa.String(length=64), nullable=True),
        sa.Column('ozon_api_key', sa.String(length=256), nullable=True),
        sa.Column('wb_api_key', sa.String(length=512), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('onboarded_at', sa.DateTime(), nullable=True),
    )

    # 2. user_id колонки + FK в основных таблицах.
    # UNIQUE на offer_id/nm_id оставляем глобальной — multi-tenant раскатим
    # композитом отдельной миграцией.
    for tbl in ('shipment_requests', 'ozon_products', 'wb_products',
                'favorite_crossdock_points'):
        with op.batch_alter_table(tbl, schema=None) as bo:
            bo.add_column(sa.Column('user_id', sa.Integer(), nullable=True))
            bo.create_index(f'ix_{tbl}_user_id', ['user_id'])
            bo.create_foreign_key(
                f'fk_{tbl}_user', 'users', ['user_id'], ['tg_id'],
                ondelete='CASCADE',
            )

    # 3. Data migration: ALLOWED_USER_ID из .env → user-запись + привязка всех данных.
    allowed_user_id = int(os.getenv('ALLOWED_USER_ID', '0') or '0')
    if allowed_user_id:
        bind.execute(text(
            "INSERT INTO users (tg_id, supplier_name, ozon_client_id, ozon_api_key, "
            "wb_api_key, created_at, onboarded_at) "
            "VALUES (:tg, :sn, :oc, :oa, :wa, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ), {
            "tg": allowed_user_id,
            "sn": os.getenv('SUPPLIER_NAME', 'ИП Баковец'),
            "oc": os.getenv('CLIEN_TID', ''),
            "oa": os.getenv('APIKEY_OZON', ''),
            "wa": os.getenv('APIKEY_WB', ''),
        })
        for tbl in ('shipment_requests', 'ozon_products', 'wb_products',
                    'favorite_crossdock_points'):
            bind.execute(text(f"UPDATE {tbl} SET user_id = :uid WHERE user_id IS NULL"),
                         {"uid": allowed_user_id})


def downgrade() -> None:
    for tbl in ('favorite_crossdock_points', 'wb_products', 'ozon_products',
                'shipment_requests'):
        with op.batch_alter_table(tbl, schema=None) as bo:
            try:
                bo.drop_constraint(f'fk_{tbl}_user', type_='foreignkey')
            except Exception:
                pass
            try:
                bo.drop_index(f'ix_{tbl}_user_id')
            except Exception:
                pass
            bo.drop_column('user_id')
    op.drop_table('users')
