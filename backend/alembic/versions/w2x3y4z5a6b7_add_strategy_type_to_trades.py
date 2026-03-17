"""add strategy_type to trades and live_trades

Revision ID: w2x3y4z5a6b7
Revises: v1w2x3y4z5a6
Create Date: 2026-03-18 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = 'w2x3y4z5a6b7'
down_revision = 'v1w2x3y4z5a6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'trades',
        sa.Column('strategy_type', sa.String(length=100), nullable=True),
    )
    op.create_index(
        'ix_trades_strategy_type',
        'trades',
        ['strategy_type'],
        unique=False,
    )

    op.add_column(
        'live_trades',
        sa.Column('strategy_type', sa.String(length=100), nullable=True),
    )
    op.create_index(
        'ix_live_trades_strategy_type',
        'live_trades',
        ['strategy_type'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_live_trades_strategy_type', table_name='live_trades')
    op.drop_column('live_trades', 'strategy_type')

    op.drop_index('ix_trades_strategy_type', table_name='trades')
    op.drop_column('trades', 'strategy_type')
