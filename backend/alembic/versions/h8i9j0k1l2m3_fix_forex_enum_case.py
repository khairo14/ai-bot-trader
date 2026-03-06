"""fix forex enum value casing (forex -> FOREX to match SQLAlchemy name convention)

Revision ID: h8i9j0k1l2m3
Revises: g7h8i9j0k1l2
Create Date: 2026-03-07
"""
from alembic import op

revision = "h8i9j0k1l2m3"
down_revision = "g7h8i9j0k1l2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The previous migration added 'forex' (lowercase) but SQLAlchemy stores
    # enum *names* (UPPERCASE) by default, so PostgreSQL rejects 'FOREX'.
    # Rename the value to match what SQLAlchemy actually sends.
    op.execute("ALTER TYPE assetclass RENAME VALUE 'forex' TO 'FOREX'")


def downgrade() -> None:
    op.execute("ALTER TYPE assetclass RENAME VALUE 'FOREX' TO 'forex'")
