"""add trailing_stop_pct to trades

Revision ID: l2m3n4o5p6q7
Revises: k1l2m3n4o5p6
Create Date: 2026-03-10
"""
from alembic import op
import sqlalchemy as sa

revision = 'l2m3n4o5p6q7'
down_revision = 'k1l2m3n4o5p6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'trades',
        sa.Column('trailing_stop_pct', sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('trades', 'trailing_stop_pct')
