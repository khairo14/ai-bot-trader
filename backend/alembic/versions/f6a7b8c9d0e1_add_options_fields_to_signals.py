"""add options fields to signals (iv_rank, delta, theta, vega, options_meta)

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-03-06
"""
from alembic import op
import sqlalchemy as sa

revision = "f6a7b8c9d0e1"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("signals", sa.Column("iv_rank",      sa.Float(), nullable=True))
    op.add_column("signals", sa.Column("delta",        sa.Float(), nullable=True))
    op.add_column("signals", sa.Column("theta",        sa.Float(), nullable=True))
    op.add_column("signals", sa.Column("vega",         sa.Float(), nullable=True))
    op.add_column("signals", sa.Column("options_meta", sa.JSON(),  nullable=True))


def downgrade() -> None:
    op.drop_column("signals", "options_meta")
    op.drop_column("signals", "vega")
    op.drop_column("signals", "theta")
    op.drop_column("signals", "delta")
    op.drop_column("signals", "iv_rank")
