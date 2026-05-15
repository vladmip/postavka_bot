"""composite UNIQUE: (user_id, offer_id) / (user_id, nm_id) / (user_id, warehouse_id)

Revision ID: b8d2e7a91f02
Revises: f656310611e6
Create Date: 2026-05-15 13:50:00.000000

Multi-tenant: глобальные UNIQUE на offer_id/nm_id ломаются если два юзера
имеют один и тот же артикул. Переводим в композит (user_id, ...).
FavoriteCrossdockPoint UNIQUE по (user_id, warehouse_id) — раньше отсутствовал.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b8d2e7a91f02'
down_revision: Union[str, None] = 'f656310611e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SQLite не умеет ALTER COLUMN/CONSTRAINT — batch_alter_table делает rebuild.
    # Старые UNIQUE индексы на offer_id/nm_id убираем, ставим composite.
    with op.batch_alter_table('ozon_products', schema=None) as bo:
        # автогенерированные UNIQUE-индексы у SQLAlchemy идут как ix_*_unique
        # или sqlite_autoindex_*. Безопаснее — пройти по batch и заменить.
        try:
            bo.drop_index('ix_ozon_products_offer_id')
        except Exception:
            pass
        bo.create_index('ix_ozon_products_offer_id', ['offer_id'])
        bo.create_unique_constraint('uq_ozon_user_offer', ['user_id', 'offer_id'])

    with op.batch_alter_table('wb_products', schema=None) as bo:
        try:
            bo.drop_index('ix_wb_products_nm_id')
        except Exception:
            pass
        bo.create_index('ix_wb_products_nm_id', ['nm_id'])
        bo.create_unique_constraint('uq_wb_user_nm', ['user_id', 'nm_id'])

    with op.batch_alter_table('favorite_crossdock_points', schema=None) as bo:
        bo.create_unique_constraint('uq_fav_user_wh', ['user_id', 'warehouse_id'])


def downgrade() -> None:
    with op.batch_alter_table('favorite_crossdock_points', schema=None) as bo:
        try:
            bo.drop_constraint('uq_fav_user_wh', type_='unique')
        except Exception:
            pass

    with op.batch_alter_table('wb_products', schema=None) as bo:
        try:
            bo.drop_constraint('uq_wb_user_nm', type_='unique')
        except Exception:
            pass

    with op.batch_alter_table('ozon_products', schema=None) as bo:
        try:
            bo.drop_constraint('uq_ozon_user_offer', type_='unique')
        except Exception:
            pass
