"""add notifications table

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-03-05
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "b2c3d4e5f6a7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Use DO block for idempotent enum creation — PostgreSQL has no
    # native "CREATE TYPE IF NOT EXISTS" syntax.
    op.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'notificationlevel') THEN
                CREATE TYPE notificationlevel AS ENUM ('info','success','warning','error');
            END IF;
        END $$;
    """)
    op.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'notificationcategory') THEN
                CREATE TYPE notificationcategory AS ENUM ('signal','trade','emergency','system','ml');
            END IF;
        END $$;
    """)
    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("level", postgresql.ENUM("info", "success", "warning", "error", name="notificationlevel", create_type=False), nullable=False, server_default="info"),
        sa.Column("category", postgresql.ENUM("signal", "trade", "emergency", "system", "ml", name="notificationcategory", create_type=False), nullable=False, server_default="system"),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("is_read", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("email_sent", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_notifications_id", "notifications", ["id"])
    op.create_index("ix_notifications_created_at", "notifications", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_notifications_created_at", table_name="notifications")
    op.drop_index("ix_notifications_id", table_name="notifications")
    op.drop_table("notifications")
    op.execute("DROP TYPE IF EXISTS notificationlevel")
    op.execute("DROP TYPE IF EXISTS notificationcategory")
