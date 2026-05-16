"""add auto_book_mode to shipment_requests

Revision ID: a92f1c5d7e84
Revises: c4f2e9b1a3d8
Create Date: 2026-05-16 11:35:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a92f1c5d7e84'
down_revision: Union[str, None] = 'c4f2e9b1a3d8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('shipment_requests', schema=None) as bo:
        bo.add_column(sa.Column('auto_book_mode', sa.String(length=8), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('shipment_requests', schema=None) as bo:
        bo.drop_column('auto_book_mode')
