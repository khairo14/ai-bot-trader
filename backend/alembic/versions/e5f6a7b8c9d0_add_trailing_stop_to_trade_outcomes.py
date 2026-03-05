"""add trailing_stop_pct to trade_outcomes

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-03-06
"""
from alembic import op
import sqlalchemy as sa

revision = "e5f6a7b8c9d0"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "trade_outcomes",
        sa.Column("trailing_stop_pct", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("trade_outcomes", "trailing_stop_pct")
