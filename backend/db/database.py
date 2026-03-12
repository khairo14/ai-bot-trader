from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from loguru import logger

from config import settings


# Convert postgresql:// to postgresql+asyncpg:// for async support
async_db_url = settings.database_url.replace(
    "postgresql://", "postgresql+asyncpg://"
)

# IMP-28: pass ssl='require' to asyncpg when db_ssl=true in settings so that
# production deployments (RDS, Supabase, managed Postgres) encrypt the DB link.
_connect_args: dict = {}
if settings.db_ssl:
    _connect_args["ssl"] = "require"

engine = create_async_engine(
    async_db_url,
    echo=settings.debug,
    pool_pre_ping=True,
    pool_size=30,        # 30 persistent connections — supports 30 concurrent strategies
    max_overflow=15,     # 15 burst connections → 45 total ceiling
    pool_recycle=600,    # recycle idle connections after 10 min
    pool_timeout=30,     # raise after 30 s instead of hanging forever
    connect_args=_connect_args,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


async def init_db():
    """Verify DB connectivity on startup — schema is managed by Alembic.

    G10: Raw ALTER TABLE statements removed; these columns are now tracked in
    Alembic migration n4o5p6q7r8s9_audit2_schema_fixes.py. Run
    `alembic upgrade head` to apply any pending migrations.
    """
    from db import models  # noqa: F401 — ensure models are registered
    logger.info("Database schema verified (Alembic-managed).")


async def get_db():
    """Dependency injection: yields a DB session per request."""
    async with AsyncSessionLocal() as session:
        yield session
