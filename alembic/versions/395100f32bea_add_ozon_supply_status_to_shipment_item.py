"""add ozon_supply_status / status_at / order_number to shipment_items

Revision ID: 395100f32bea
Revises: e799858ae69c
Create Date: 2026-05-15 08:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '395100f32bea'
down_revision: Union[str, None] = 'e799858ae69c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('shipment_items', schema=None) as batch_op:
        batch_op.add_column(sa.Column('ozon_supply_status', sa.String(length=48), nullable=True))
        batch_op.add_column(sa.Column('ozon_supply_status_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('ozon_order_number', sa.String(length=32), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('shipment_items', schema=None) as batch_op:
        batch_op.drop_column('ozon_order_number')
        batch_op.drop_column('ozon_supply_status_at')
        batch_op.drop_column('ozon_supply_status')
