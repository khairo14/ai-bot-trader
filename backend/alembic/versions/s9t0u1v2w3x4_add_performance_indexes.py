"""add performance indexes for common filter columns

Revision ID: s9t0u1v2w3x4
Revises: r8s9t0u1v2w3
Create Date: 2025-01-01 00:00:00.000000

M-1 FIX: Add missing indexes for frequently-filtered columns.
Existing indexes (from prior migrations) already cover:
  - trades: (status, is_paper) composite, broker, strategy_name
  - signals: dismissed, acted_on

This migration adds the remaining high-value indexes:
  - trades.opened_at  — ORDER BY / range queries in portfolio + forward-test
  - trades.closed_at  — daily P&L window in portfolio_summary
  - signals.strategy_name — per-strategy signal lookups
  - notifications.is_read — unread-count queries run on every dashboard load
"""
from alembic import op


# revision identifiers, used by Alembic.
revision = 's9t0u1v2w3x4'
down_revision = 'r8s9t0u1v2w3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_trades_opened_at", "trades", ["opened_at"])
    op.create_index("ix_trades_closed_at", "trades", ["closed_at"])
    op.create_index("ix_signals_strategy_name", "signals", ["strategy_name"])
    op.create_index("ix_notifications_is_read", "notifications", ["is_read"])


def downgrade() -> None:
    op.drop_index("ix_notifications_is_read", table_name="notifications")
    op.drop_index("ix_signals_strategy_name", table_name="signals")
    op.drop_index("ix_trades_closed_at", table_name="trades")
    op.drop_index("ix_trades_opened_at", table_name="trades")
