import os
import sys
from logging.config import fileConfig

from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool
from alembic import context

# ── Load .env from repo root (two levels above alembic/) ─────────────────────
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

# ── Put backend/ on sys.path so we can import app modules ────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# ── Import Base + all models so their metadata is registered ─────────────────
from db.database import Base  # noqa: E402
import db.models  # noqa: E402, F401  (registers Signal, Trade, Strategy, BacktestResult)

# ── Alembic config object ─────────────────────────────────────────────────────
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# ── Override DB URL: strip +asyncpg so Alembic uses the psycopg2 driver ──────
_raw_url = os.getenv("DATABASE_URL", "postgresql://trader:trader_password@localhost:5432/ai_trader")
_sync_url = _raw_url.replace("postgresql+asyncpg://", "postgresql://")
config.set_main_option("sqlalchemy.url", _sync_url)


def run_migrations_offline() -> None:
    """Emit SQL to stdout; no live DB connection required."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations against a live DB connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
