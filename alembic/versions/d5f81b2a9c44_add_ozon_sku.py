"""add ozon_sku numeric id

Revision ID: d5f81b2a9c44
Revises: c4d2f8a9e7b3
Create Date: 2026-05-11 19:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d5f81b2a9c44"
down_revision: Union[str, None] = "c4d2f8a9e7b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("skus") as batch_op:
        batch_op.add_column(sa.Column("ozon_sku", sa.Integer(), nullable=True))
        batch_op.create_index("ix_skus_ozon_sku", ["ozon_sku"])


def downgrade() -> None:
    with op.batch_alter_table("skus") as batch_op:
        batch_op.drop_index("ix_skus_ozon_sku")
        batch_op.drop_column("ozon_sku")
