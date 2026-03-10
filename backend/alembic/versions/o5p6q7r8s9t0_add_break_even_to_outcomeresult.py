"""add BREAK_EVEN to outcomeresult enum

Revision ID: o5p6q7r8s9t0
Revises: n4o5p6q7r8s9
Create Date: 2026-03-10

"""
from alembic import op

# revision identifiers, used by Alembic.
revision = 'o5p6q7r8s9t0'
down_revision = 'n4o5p6q7r8s9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ADD VALUE is transactional in PG 12+; IF NOT EXISTS makes it idempotent.
    op.execute("ALTER TYPE outcomeresult ADD VALUE IF NOT EXISTS 'BREAK_EVEN'")


def downgrade() -> None:
    # PostgreSQL does not support removing enum values; downgrade is a no-op.
    pass
