"""add broker_risk_settings table

Revision ID: k1l2m3n4o5p6
Revises: j0k1l2m3n4o5
Create Date: 2026-03-07
"""
from alembic import op
import sqlalchemy as sa

revision = 'k1l2m3n4o5p6'
down_revision = 'j0k1l2m3n4o5'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Use raw SQL so we reference the existing brokername enum without recreating it
    op.execute("""
        CREATE TABLE broker_risk_settings (
            id SERIAL PRIMARY KEY,
            broker brokername NOT NULL,
            risk_per_trade_pct FLOAT,
            max_open_positions INTEGER,
            daily_circuit_breaker_pct FLOAT,
            max_consecutive_losses INTEGER,
            max_exposure_per_asset_pct FLOAT,
            max_exposure_per_class_pct FLOAT,
            updated_at TIMESTAMP,
            CONSTRAINT uq_broker_risk_settings_broker UNIQUE (broker)
        )
    """)
    op.create_index('ix_broker_risk_settings_broker', 'broker_risk_settings', ['broker'])
    op.create_index('ix_broker_risk_settings_id', 'broker_risk_settings', ['id'])


def downgrade() -> None:
    op.drop_index('ix_broker_risk_settings_id', table_name='broker_risk_settings')
    op.drop_index('ix_broker_risk_settings_broker', table_name='broker_risk_settings')
    op.drop_table('broker_risk_settings')
    # do NOT drop the 'brokername' enum — it is shared with other tables
