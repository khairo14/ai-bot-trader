"""add live_trades table

Revision ID: f1a2b3c4d5e6
Revises: e5f6a7b8c9d0
Create Date: 2026-03-10

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ENUM as pgENUM

# revision identifiers, used by Alembic.
revision = 'f1a2b3c4d5e6'
down_revision = 'p6q7r8s9t0u1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Re-use the existing PostgreSQL enum types — DO NOT create them again.
    # Using postgresql.ENUM(create_type=False) instead of sa.Enum(create_type=False)
    # because sa.Enum can still fire the _on_table_create DDL hook in some
    # SQLAlchemy versions, causing "type already exists".  postgresql.ENUM with
    # create_type=False unconditionally skips all CREATE TYPE DDL.
    _orderstatus   = pgENUM(name='orderstatus',   create_type=False)
    _executionmode = pgENUM(name='executionmode', create_type=False)
    _brokername    = pgENUM(name='brokername',    create_type=False)
    _assetclass    = pgENUM(name='assetclass',    create_type=False)

    op.create_table(
        'live_trades',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('signal_id', sa.Integer(), sa.ForeignKey('signals.id'), nullable=True),
        sa.Column('symbol', sa.String(20), nullable=False),
        sa.Column('side', sa.String(10), nullable=False),
        sa.Column('quantity', sa.Float(), nullable=False),
        sa.Column('entry_price', sa.Float(), nullable=True),
        sa.Column('exit_price', sa.Float(), nullable=True),
        sa.Column('stop_loss', sa.Float(), nullable=True),
        sa.Column('take_profit', sa.Float(), nullable=True),
        sa.Column('trailing_stop_pct', sa.Float(), nullable=True),
        sa.Column('pnl', sa.Float(), nullable=True),
        sa.Column('pnl_pct', sa.Float(), nullable=True),
        sa.Column('status', _orderstatus, nullable=False),
        sa.Column('execution_mode', _executionmode, nullable=False),
        sa.Column('broker', _brokername, nullable=False),
        sa.Column('asset_class', _assetclass, nullable=False),
        sa.Column('is_paper', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('broker_order_id', sa.String(100), nullable=True),
        sa.Column('strategy_name', sa.String(100), nullable=True),
        sa.Column('notes', sa.String(500), nullable=True),
        sa.Column('opened_at', sa.DateTime(), nullable=True),
        sa.Column('closed_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
    )
    op.create_index(op.f('ix_live_trades_id'),            'live_trades', ['id'],            unique=False)
    op.create_index(op.f('ix_live_trades_symbol'),        'live_trades', ['symbol'],        unique=False)
    op.create_index(op.f('ix_live_trades_broker'),        'live_trades', ['broker'],        unique=False)
    op.create_index(op.f('ix_live_trades_strategy_name'), 'live_trades', ['strategy_name'], unique=False)
    op.create_index(op.f('ix_live_trades_user_id'),       'live_trades', ['user_id'],       unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_live_trades_user_id'),       table_name='live_trades')
    op.drop_index(op.f('ix_live_trades_strategy_name'), table_name='live_trades')
    op.drop_index(op.f('ix_live_trades_broker'),        table_name='live_trades')
    op.drop_index(op.f('ix_live_trades_symbol'),        table_name='live_trades')
    op.drop_index(op.f('ix_live_trades_id'),            table_name='live_trades')
    op.drop_table('live_trades')
