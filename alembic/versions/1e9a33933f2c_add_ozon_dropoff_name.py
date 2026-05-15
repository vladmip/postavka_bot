"""add ozon_dropoff_name to shipment_items

Revision ID: 1e9a33933f2c
Revises: 395100f32bea
Create Date: 2026-05-15 08:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '1e9a33933f2c'
down_revision: Union[str, None] = '395100f32bea'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('shipment_items', schema=None) as batch_op:
        batch_op.add_column(sa.Column('ozon_dropoff_name', sa.String(length=128), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('shipment_items', schema=None) as batch_op:
        batch_op.drop_column('ozon_dropoff_name')
