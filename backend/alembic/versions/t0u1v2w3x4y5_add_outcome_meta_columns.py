"""add options_meta and signal_type_category to trade_outcomes

Revision ID: t0u1v2w3x4y5
Revises: s9t0u1v2w3x4
Create Date: 2026-01-01 00:00:00.000000

GAP-06: options_meta JSON stores strikes/expiry/legs for options ML labels.
IMP-04: signal_type_category disambiguates ML labels — "directional" vs "premium_collection".
"""
from alembic import op
import sqlalchemy as sa

revision = 't0u1v2w3x4y5'
down_revision = 's9t0u1v2w3x4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'trade_outcomes',
        sa.Column('options_meta', sa.JSON(), nullable=True),
    )
    op.add_column(
        'trade_outcomes',
        sa.Column('signal_type_category', sa.String(30), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('trade_outcomes', 'signal_type_category')
    op.drop_column('trade_outcomes', 'options_meta')
