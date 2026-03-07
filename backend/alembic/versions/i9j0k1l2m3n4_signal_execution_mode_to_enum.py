"""Convert signals.execution_mode from String to ExecutionMode enum

Revision ID: i9j0k1l2m3n4
Revises: h8i9j0k1l2m3
Create Date: 2026-03-08

"""
from alembic import op
import sqlalchemy as sa

revision = "i9j0k1l2m3n4"
down_revision = "h8i9j0k1l2m3"
branch_labels = None
depends_on = None

_VALID = ("suggestion", "semi-auto", "full-auto")


def upgrade() -> None:
    # 1. Create the enum type (may already exist from strategies table — IF NOT EXISTS guard)
    op.execute(
        "DO $$ BEGIN "
        "  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'executionmode') THEN "
        "    CREATE TYPE executionmode AS ENUM ('suggestion', 'semi-auto', 'full-auto'); "
        "  END IF; "
        "END $$;"
    )
    # 2. Normalise any out-of-range strings to NULL before casting
    op.execute(
        f"UPDATE signals SET execution_mode = NULL "
        f"WHERE execution_mode NOT IN {_VALID!r}"
    )
    # 3. Cast the column to the enum type
    op.execute(
        "ALTER TABLE signals "
        "ALTER COLUMN execution_mode TYPE executionmode "
        "USING execution_mode::executionmode"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE signals "
        "ALTER COLUMN execution_mode TYPE VARCHAR(20) "
        "USING execution_mode::VARCHAR"
    )
