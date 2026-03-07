from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from loguru import logger

from config import settings


# Convert postgresql:// to postgresql+asyncpg:// for async support
async_db_url = settings.database_url.replace(
    "postgresql://", "postgresql+asyncpg://"
)

engine = create_async_engine(
    async_db_url,
    echo=settings.debug,
    pool_pre_ping=True,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


async def init_db():
    """Apply safe additive column migrations only — schema is managed by Alembic."""
    from db import models  # noqa: F401 — ensure models are registered
    from sqlalchemy import text
    # NOTE: create_all() is intentionally removed — use `alembic upgrade head` for schema.
    # Only idempotent ADD COLUMN IF NOT EXISTS statements are allowed here.
    async with engine.begin() as conn:
        safe_alters = [
            "ALTER TABLE trades ADD COLUMN IF NOT EXISTS notes VARCHAR(500)",
            "ALTER TABLE signals ADD COLUMN IF NOT EXISTS dismissed BOOLEAN DEFAULT FALSE",
        ]
        for stmt in safe_alters:
            try:
                await conn.execute(text(stmt))
            except Exception as e:
                logger.warning(f"[DB] Column migration skipped ({stmt}): {e}")
    logger.info("Database schema verified (Alembic-managed).")


async def get_db():
    """Dependency injection: yields a DB session per request."""
    async with AsyncSessionLocal() as session:
        yield session
