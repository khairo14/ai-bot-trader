"""add unique constraint on signal_id to trades and live_trades

Revision ID: r8s9t0u1v2w3
Revises: q7r8s9t0u1v2
Create Date: 2025-01-01 00:00:00.000000

Signal.trade and Signal.live_trade relationships use uselist=False, which
assumes at most one Trade/LiveTrade per Signal. Without a unique constraint
this invariant can be silently violated (e.g. ghost-trades retried before the
duplicate-guard was in place), causing relationship queries to silently return
only the first matching row and masking the duplicate.

This migration:
1. Deduplicates any existing duplicate signal_id rows — keeps the highest-id
   row (most recent) per signal_id, consistent with how ForwardEngine resolves
   races by keeping the newest record.
2. Adds a unique constraint on trades.signal_id and live_trades.signal_id.
   NULL values are not constrained (multiple NULL signal_ids are still allowed
   for manually-entered or broker-imported trades).
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = 'r8s9t0u1v2w3'
down_revision = 'q7r8s9t0u1v2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Step 1: remove duplicate rows — keep the highest-id (most recent) Trade
    # per signal_id so the unique constraint can be applied cleanly.
    op.execute("""
        DELETE FROM trades
        WHERE id NOT IN (
            SELECT MAX(id)
            FROM trades
            WHERE signal_id IS NOT NULL
            GROUP BY signal_id
        )
        AND signal_id IS NOT NULL
    """)

    # Step 2: same deduplicate for live_trades
    op.execute("""
        DELETE FROM live_trades
        WHERE id NOT IN (
            SELECT MAX(id)
            FROM live_trades
            WHERE signal_id IS NOT NULL
            GROUP BY signal_id
        )
        AND signal_id IS NOT NULL
    """)

    # Step 3: add unique constraints (NULL-safe — PostgreSQL allows multiple NULLs)
    op.create_unique_constraint("uq_trades_signal_id", "trades", ["signal_id"])
    op.create_unique_constraint("uq_live_trades_signal_id", "live_trades", ["signal_id"])


def downgrade() -> None:
    op.drop_constraint("uq_live_trades_signal_id", "live_trades", type_="unique")
    op.drop_constraint("uq_trades_signal_id", "trades", type_="unique")
    # Deleted duplicate rows are not restored on downgrade — data loss is
    # acceptable here since duplicate trades were ghost-records from a fixed bug.
