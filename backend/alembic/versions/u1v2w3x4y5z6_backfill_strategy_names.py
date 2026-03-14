"""backfill strategy_name from user-defined Strategy.name

Previously the signal_runner stored the algorithm type (e.g. 'hybrid_macd_rsi')
as strategy_name on signals, trades, live_trades, and trade_outcomes.  This
migration updates those rows to use the user-defined Strategy.name instead,
matching on (broker, parameters->>'symbol', parameters->>'timeframe',
parameters->>'strategy_type').

Revision ID: u1v2w3x4y5z6
Revises: t0u1v2w3x4y5
Create Date: 2026-03-14 00:00:00.000000
"""
from alembic import op

revision = 'u1v2w3x4y5z6'
down_revision = 't0u1v2w3x4y5'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── signals ───────────────────────────────────────────────────────────────
    # Match on (broker, symbol, timeframe, strategy_type) — most precise.
    op.execute("""
        UPDATE signals s
        SET strategy_name = (
            SELECT st.name
            FROM strategies st
            WHERE st.broker::text = s.broker::text
              AND st.parameters->>'symbol' = s.symbol
              AND COALESCE(st.parameters->>'timeframe', '1h') = s.timeframe
              AND s.strategy_name = COALESCE(
                    st.parameters->>'strategy_type',
                    st.parameters->>'strategy_name'
                  )
            LIMIT 1
        )
        WHERE EXISTS (
            SELECT 1
            FROM strategies st
            WHERE st.broker::text = s.broker::text
              AND st.parameters->>'symbol' = s.symbol
              AND COALESCE(st.parameters->>'timeframe', '1h') = s.timeframe
              AND s.strategy_name = COALESCE(
                    st.parameters->>'strategy_type',
                    st.parameters->>'strategy_name'
                  )
        )
    """)

    # ── trades ────────────────────────────────────────────────────────────────
    # trades has no timeframe column — match on (broker, symbol, strategy_type).
    op.execute("""
        UPDATE trades t
        SET strategy_name = (
            SELECT st.name
            FROM strategies st
            WHERE st.broker::text = t.broker::text
              AND st.parameters->>'symbol' = t.symbol
              AND t.strategy_name = COALESCE(
                    st.parameters->>'strategy_type',
                    st.parameters->>'strategy_name'
                  )
            LIMIT 1
        )
        WHERE t.strategy_name IS NOT NULL
          AND EXISTS (
            SELECT 1
            FROM strategies st
            WHERE st.broker::text = t.broker::text
              AND st.parameters->>'symbol' = t.symbol
              AND t.strategy_name = COALESCE(
                    st.parameters->>'strategy_type',
                    st.parameters->>'strategy_name'
                  )
          )
    """)

    # ── live_trades ───────────────────────────────────────────────────────────
    op.execute("""
        UPDATE live_trades t
        SET strategy_name = (
            SELECT st.name
            FROM strategies st
            WHERE st.broker::text = t.broker::text
              AND st.parameters->>'symbol' = t.symbol
              AND t.strategy_name = COALESCE(
                    st.parameters->>'strategy_type',
                    st.parameters->>'strategy_name'
                  )
            LIMIT 1
        )
        WHERE t.strategy_name IS NOT NULL
          AND EXISTS (
            SELECT 1
            FROM strategies st
            WHERE st.broker::text = t.broker::text
              AND st.parameters->>'symbol' = t.symbol
              AND t.strategy_name = COALESCE(
                    st.parameters->>'strategy_type',
                    st.parameters->>'strategy_name'
                  )
          )
    """)

    # ── trade_outcomes ────────────────────────────────────────────────────────
    # Join through signals to get broker + symbol + timeframe for matching.
    op.execute("""
        UPDATE trade_outcomes o
        SET strategy_name = (
            SELECT st.name
            FROM strategies st
            JOIN signals s ON o.signal_id = s.id
            WHERE st.broker::text = s.broker::text
              AND st.parameters->>'symbol' = s.symbol
              AND COALESCE(st.parameters->>'timeframe', '1h') = s.timeframe
              AND o.strategy_name = COALESCE(
                    st.parameters->>'strategy_type',
                    st.parameters->>'strategy_name'
                  )
            LIMIT 1
        )
        WHERE o.signal_id IS NOT NULL
          AND EXISTS (
            SELECT 1
            FROM strategies st
            JOIN signals s ON o.signal_id = s.id
            WHERE st.broker::text = s.broker::text
              AND st.parameters->>'symbol' = s.symbol
              AND COALESCE(st.parameters->>'timeframe', '1h') = s.timeframe
              AND o.strategy_name = COALESCE(
                    st.parameters->>'strategy_type',
                    st.parameters->>'strategy_name'
                  )
          )
    """)


def downgrade() -> None:
    # Reversing this migration would require knowing the original algorithm type
    # per row, which is not stored after the update. Downgrade is intentionally
    # a no-op — the original values are already in production and not worth restoring.
    pass
