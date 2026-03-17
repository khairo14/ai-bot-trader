"""add strategy_type to signals

Revision ID: v1w2x3y4z5a6
Revises: u1v2w3x4y5z6
Create Date: 2026-03-17 07:30:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = 'v1w2x3y4z5a6'
down_revision = 'u1v2w3x4y5z6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'signals',
        sa.Column('strategy_type', sa.String(length=100), nullable=True),
    )
    op.create_index(
        'ix_signals_strategy_type',
        'signals',
        ['strategy_type'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_signals_strategy_type', table_name='signals')
    op.drop_column('signals', 'strategy_type')
