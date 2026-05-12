"""add slot_date_to to supplies

Revision ID: a3f9c7d21e45
Revises: 1712be522354
Create Date: 2026-05-11 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a3f9c7d21e45"
down_revision: Union[str, None] = "1712be522354"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("supplies") as batch_op:
        batch_op.add_column(sa.Column("slot_date_to", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("supplies") as batch_op:
        batch_op.drop_column("slot_date_to")
