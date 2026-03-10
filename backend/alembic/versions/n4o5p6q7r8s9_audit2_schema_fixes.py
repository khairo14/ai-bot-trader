"""audit2 schema fixes: ix_signals_created_at, trades.notes, signals.dismissed

Revision ID: n4o5p6q7r8s9
Revises: l2m3n4o5p6q7
Create Date: 2025-01-01 00:00:00.000000

G7  – Index on signals.created_at speeds up dedup window queries.
G10 – trades.notes and signals.dismissed are now tracked by Alembic instead
      of being created by raw ALTER TABLE in init_db().  The columns may
      already exist in production databases that ran the old init_db(); the
      upgrade() therefore uses IF NOT EXISTS / try-except guards.
I2  – signals.created_at changed to NOT NULL (existing NULLs are back-filled
      with the current timestamp before the constraint is applied).
"""
from alembic import op
import sqlalchemy as sa

revision = 'n4o5p6q7r8s9'
down_revision = 'l2m3n4o5p6q7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── G10: trades.notes ──────────────────────────────────────────────────
    # Column may already exist if init_db() ran the now-removed ALTER TABLE.
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    trades_cols = {c["name"] for c in inspector.get_columns("trades")}
    if "notes" not in trades_cols:
        op.add_column("trades", sa.Column("notes", sa.String(500), nullable=True))

    # ── G10: signals.dismissed ─────────────────────────────────────────────
    signals_cols = {c["name"] for c in inspector.get_columns("signals")}
    if "dismissed" not in signals_cols:
        op.add_column(
            "signals",
            sa.Column("dismissed", sa.Boolean(), server_default="false", nullable=False),
        )

    # ── I2: signals.created_at NOT NULL ───────────────────────────────────
    # Back-fill any existing NULLs first, then tighten the constraint.
    op.execute(
        "UPDATE signals SET created_at = now() WHERE created_at IS NULL"
    )
    op.alter_column(
        "signals",
        "created_at",
        existing_type=sa.DateTime(),
        nullable=False,
    )

    # ── G7: index on signals.created_at ───────────────────────────────────
    existing_indexes = {i["name"] for i in inspector.get_indexes("signals")}
    if "ix_signals_created_at" not in existing_indexes:
        op.create_index("ix_signals_created_at", "signals", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_signals_created_at", table_name="signals")
    op.alter_column(
        "signals",
        "created_at",
        existing_type=sa.DateTime(),
        nullable=True,
    )
    op.drop_column("signals", "dismissed")
    op.drop_column("trades", "notes")
