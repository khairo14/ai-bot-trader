"""Add user_id FK to signals and trades for data-level multi-tenancy (F-059)

Revision ID: j0k1l2m3n4o5
Revises: i9j0k1l2m3n4
Create Date: 2026-03-08
"""
from alembic import op
import sqlalchemy as sa

revision = "j0k1l2m3n4o5"
down_revision = "i9j0k1l2m3n4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("signals", sa.Column("user_id", sa.Integer(), nullable=True))
    op.create_index("ix_signals_user_id", "signals", ["user_id"])
    op.create_foreign_key(
        "fk_signals_user_id", "signals", "users", ["user_id"], ["id"],
        ondelete="SET NULL",
    )
    op.add_column("trades", sa.Column("user_id", sa.Integer(), nullable=True))
    op.create_index("ix_trades_user_id", "trades", ["user_id"])
    op.create_foreign_key(
        "fk_trades_user_id", "trades", "users", ["user_id"], ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_trades_user_id", "trades", type_="foreignkey")
    op.drop_index("ix_trades_user_id", "trades")
    op.drop_column("trades", "user_id")
    op.drop_constraint("fk_signals_user_id", "signals", type_="foreignkey")
    op.drop_index("ix_signals_user_id", "signals")
    op.drop_column("signals", "user_id")
