"""add execution_mode to signals

Revision ID: a1b2c3d4e5f6
Revises: 499535e395ea
Create Date: 2026-03-05 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '499535e395ea'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'signals',
        sa.Column('execution_mode', sa.String(length=20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('signals', 'execution_mode')
