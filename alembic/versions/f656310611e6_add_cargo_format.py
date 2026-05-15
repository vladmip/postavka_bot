"""add cargo_format to shipment_requests

Revision ID: f656310611e6
Revises: 3eab6656f631
Create Date: 2026-05-15 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f656310611e6'
down_revision: Union[str, None] = '3eab6656f631'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('shipment_requests', schema=None) as bo:
        bo.add_column(sa.Column('cargo_format', sa.String(length=16), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('shipment_requests', schema=None) as bo:
        bo.drop_column('cargo_format')
