"""add indexes to trades

Revision ID: p6q7r8s9t0u1
Revises: o5p6q7r8s9t0
Create Date: 2025-01-01 00:00:00.000000

"""
from alembic import op

# revision identifiers, used by Alembic.
revision = 'p6q7r8s9t0u1'
down_revision = 'o5p6q7r8s9t0'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_trades_broker", "trades", ["broker"])
    op.create_index("ix_trades_strategy_name", "trades", ["strategy_name"])
    op.create_index("ix_trades_status_is_paper", "trades", ["status", "is_paper"])


def downgrade() -> None:
    op.drop_index("ix_trades_status_is_paper", table_name="trades")
    op.drop_index("ix_trades_strategy_name", table_name="trades")
    op.drop_index("ix_trades_broker", table_name="trades")
