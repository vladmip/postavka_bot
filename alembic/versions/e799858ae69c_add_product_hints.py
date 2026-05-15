"""add product_hints table

Revision ID: e799858ae69c
Revises: 390d74561795
Create Date: 2026-05-15 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e799858ae69c'
down_revision: Union[str, None] = '390d74561795'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'product_hints',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('ozon_product_id', sa.Integer(), nullable=False),
        sa.Column('packaging', sa.Text(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ['ozon_product_id'], ['ozon_products.id'], ondelete='CASCADE',
        ),
        sa.UniqueConstraint('ozon_product_id', name='uq_product_hints_ozon_product_id'),
    )
    op.create_index(
        'ix_product_hints_ozon_product_id', 'product_hints', ['ozon_product_id'],
    )


def downgrade() -> None:
    op.drop_index('ix_product_hints_ozon_product_id', table_name='product_hints')
    op.drop_table('product_hints')
