"""add forex to assetclass enum

Revision ID: g7h8i9j0k1l2
Revises: f6a7b8c9d0e1
Create Date: 2026-03-07
"""
from alembic import op
import sqlalchemy as sa

revision = "g7h8i9j0k1l2"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # PostgreSQL requires COMMIT before ALTER TYPE ADD VALUE,
    # which Alembic handles via execute_if / raw SQL.
    op.execute("ALTER TYPE assetclass ADD VALUE IF NOT EXISTS 'forex'")


def downgrade() -> None:
    # PostgreSQL does not support removing enum values; a full recreation
    # would be required. Leave as no-op — the value simply won't be used.
    pass
