"""add slot_dates_json + ozon_offer_id + wb_nm_id

Revision ID: b8e1a4c52d11
Revises: a3f9c7d21e45
Create Date: 2026-05-11 19:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b8e1a4c52d11"
down_revision: Union[str, None] = "a3f9c7d21e45"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("supplies") as batch_op:
        batch_op.add_column(sa.Column("slot_dates_json", sa.JSON(), nullable=True))

    with op.batch_alter_table("skus") as batch_op:
        batch_op.add_column(sa.Column("ozon_offer_id", sa.String(128), nullable=True))
        batch_op.add_column(sa.Column("wb_nm_id", sa.Integer(), nullable=True))
        batch_op.create_index("ix_skus_ozon_offer_id", ["ozon_offer_id"])
        batch_op.create_index("ix_skus_wb_nm_id", ["wb_nm_id"])


def downgrade() -> None:
    with op.batch_alter_table("skus") as batch_op:
        batch_op.drop_index("ix_skus_wb_nm_id")
        batch_op.drop_index("ix_skus_ozon_offer_id")
        batch_op.drop_column("wb_nm_id")
        batch_op.drop_column("ozon_offer_id")

    with op.batch_alter_table("supplies") as batch_op:
        batch_op.drop_column("slot_dates_json")
