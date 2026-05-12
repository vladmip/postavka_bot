"""shipment_requests + shipment_items

Revision ID: c4d2f8a9e7b3
Revises: b8e1a4c52d11
Create Date: 2026-05-11 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c4d2f8a9e7b3"
down_revision: Union[str, None] = "b8e1a4c52d11"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "shipment_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("state", sa.String(32), nullable=False, server_default="draft"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("target_date_from", sa.DateTime(), nullable=True),
        sa.Column("target_date_to", sa.DateTime(), nullable=True),
        sa.Column("crossdock_warehouses_json", sa.JSON(), nullable=True),
        sa.Column("source_files_json", sa.JSON(), nullable=True),
        sa.Column("comments", sa.Text(), nullable=True),
    )
    op.create_index("ix_shipment_requests_state", "shipment_requests", ["state"])

    op.create_table(
        "shipment_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("request_id", sa.Integer(),
                  sa.ForeignKey("shipment_requests.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sku_id", sa.Integer(), sa.ForeignKey("skus.id"), nullable=True),
        sa.Column("raw_article", sa.String(128), nullable=False),
        sa.Column("marketplace", sa.String(8), nullable=False),
        sa.Column("cluster", sa.String(64), nullable=False),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.Column("target_warehouse", sa.String(128), nullable=True),
        sa.Column("booked_supply_id", sa.String(64), nullable=True),
        sa.Column("booked_slot_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_shipment_items_request_id", "shipment_items", ["request_id"])


def downgrade() -> None:
    op.drop_index("ix_shipment_items_request_id", "shipment_items")
    op.drop_table("shipment_items")
    op.drop_index("ix_shipment_requests_state", "shipment_requests")
    op.drop_table("shipment_requests")
