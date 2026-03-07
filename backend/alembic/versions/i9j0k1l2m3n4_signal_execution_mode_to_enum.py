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
    # The 'executionmode' PostgreSQL type was created by the initial migration with
    # UPPERCASE names: ('SUGGESTION', 'SEMI_AUTO', 'FULL_AUTO').
    # The signals.execution_mode column was a String(20) and stored lowercase .value
    # strings (e.g. 'suggestion', 'semi-auto', 'full-auto') written directly by
    # application code.  We must map them to the uppercase DB names before casting.
    #
    # Mapping:
    #   'suggestion'  → 'SUGGESTION'
    #   'semi-auto'   → 'SEMI_AUTO'   (Python value uses dash; DB name uses underscore)
    #   'full-auto'   → 'FULL_AUTO'
    #   already uppercase values pass through unchanged
    #   anything else → NULL (safe default for nullable column)

    op.execute(
        "UPDATE signals SET execution_mode = "
        "  CASE "
        "    WHEN execution_mode = 'suggestion'  THEN 'SUGGESTION' "
        "    WHEN execution_mode = 'semi-auto'   THEN 'SEMI_AUTO'  "
        "    WHEN execution_mode = 'full-auto'   THEN 'FULL_AUTO'  "
        "    WHEN execution_mode = 'SUGGESTION'  THEN 'SUGGESTION' "
        "    WHEN execution_mode = 'SEMI_AUTO'   THEN 'SEMI_AUTO'  "
        "    WHEN execution_mode = 'FULL_AUTO'   THEN 'FULL_AUTO'  "
        "    ELSE NULL "
        "  END"
    )

    # Cast the column from String to the existing executionmode enum type.
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
