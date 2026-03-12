"""add indexes to signals dismissed and acted_on

Revision ID: q7r8s9t0u1v2
Revises: p6q7r8s9t0u1
Create Date: 2025-01-01 00:00:00.000000

IMP-24: signals.dismissed and signals.acted_on are filtered in every dashboard
query but had no index. As the signals table grows (100k+ rows/year), full
table scans become noticeably slow. These partial-index-style btree indexes
cover the common query patterns:
  - WHERE dismissed = false (active signals list)
  - WHERE acted_on = false  (pending signals awaiting execution)
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = 'q7r8s9t0u1v2'
down_revision = 'g2h3i4j5k6l7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_signals_dismissed", "signals", ["dismissed"])
    op.create_index("ix_signals_acted_on",  "signals", ["acted_on"])


def downgrade() -> None:
    op.drop_index("ix_signals_acted_on",  table_name="signals")
    op.drop_index("ix_signals_dismissed", table_name="signals")
