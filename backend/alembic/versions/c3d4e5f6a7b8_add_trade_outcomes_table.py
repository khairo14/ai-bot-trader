"""add trade_outcomes table

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-03-05 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = 'c3d4e5f6a7b8'
down_revision = 'b2c3d4e5f6a7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'trade_outcomes',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('signal_id', sa.Integer(), sa.ForeignKey('signals.id'), nullable=True),
        sa.Column('symbol', sa.String(20), nullable=False),
        sa.Column('timeframe', sa.String(10), nullable=False),
        sa.Column('strategy_name', sa.String(100), nullable=False),
        sa.Column('signal_type', sa.String(10), nullable=False),
        sa.Column('entry_price', sa.Float(), nullable=False),
        sa.Column('stop_loss', sa.Float(), nullable=True),
        sa.Column('take_profit', sa.Float(), nullable=True),
        sa.Column('exit_price', sa.Float(), nullable=True),
        sa.Column('outcome', sa.Enum('win', 'loss', 'break_even', 'expired', name='outcomeresult'), nullable=True),
        sa.Column('pnl_pct', sa.Float(), nullable=True),
        sa.Column('candles_held', sa.Integer(), nullable=True),
        sa.Column('ml_label', sa.Integer(), nullable=True),
        sa.Column('resolved', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('resolved_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('signal_id'),
    )
    op.create_index('ix_trade_outcomes_id', 'trade_outcomes', ['id'])
    op.create_index('ix_trade_outcomes_symbol', 'trade_outcomes', ['symbol'])
    op.create_index('ix_trade_outcomes_resolved', 'trade_outcomes', ['resolved'])
    op.create_index('ix_trade_outcomes_created_at', 'trade_outcomes', ['created_at'])
    op.create_index('ix_trade_outcomes_signal_id', 'trade_outcomes', ['signal_id'])


def downgrade() -> None:
    op.drop_index('ix_trade_outcomes_signal_id', table_name='trade_outcomes')
    op.drop_index('ix_trade_outcomes_created_at', table_name='trade_outcomes')
    op.drop_index('ix_trade_outcomes_resolved', table_name='trade_outcomes')
    op.drop_index('ix_trade_outcomes_symbol', table_name='trade_outcomes')
    op.drop_index('ix_trade_outcomes_id', table_name='trade_outcomes')
    op.drop_table('trade_outcomes')
    op.execute("DROP TYPE IF EXISTS outcomeresult")
