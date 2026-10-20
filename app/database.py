"""
Voice Agent Platform — Async Database Configuration

Sets up async SQLAlchemy engine and session factory for PostgreSQL.
Uses asyncpg as the async driver for maximum performance.

Usage:
    from app.database import get_db

    @router.get("/example")
    async def example(db: AsyncSession = Depends(get_db)):
        result = await db.execute(select(Tenant))
        ...
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings
from app.logger import get_logger

logger = get_logger(__name__)


class Base(DeclarativeBase):
    """
    Base class for all SQLAlchemy ORM models.

    All models will inherit from this base, which provides:
    - Metadata for Alembic auto-generation
    - Common table args (if needed later)
    """

    pass


def create_engine():
    """
    Create the async SQLAlchemy engine.

    Connection pool is configured for concurrent async usage:
    - pool_size=20: Maintain 20 persistent connections
    - max_overflow=10: Allow up to 10 additional connections under load
    - pool_pre_ping=True: Verify connections are alive before using them
    """
    settings = get_settings()

    engine = create_async_engine(
        settings.DATABASE_URL,
        echo=settings.DEBUG,  # Log SQL statements in debug mode
        pool_size=20,
        max_overflow=10,
        pool_pre_ping=True,
        pool_recycle=3600,  # Recycle connections after 1 hour
    )

    logger.info(
        "database_engine_created",
        pool_size=20,
        max_overflow=10,
    )

    return engine


# Create engine and session factory at module level
engine = create_engine()

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,  # Don't expire objects after commit (better for async)
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency that provides an async database session.

    Yields a session and ensures it's properly closed after the request,
    even if an exception occurs.

    Usage:
        @router.get("/items")
        async def get_items(db: AsyncSession = Depends(get_db)):
            ...
    """
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def verify_database_connection() -> bool:
    """
    Verify that the database is reachable.

    Used during application startup and health checks.
    Returns True if connection succeeds, False otherwise.
    """
    try:
        async with engine.begin() as conn:
            await conn.execute(
                # Use text() for raw SQL
                __import__("sqlalchemy").text("SELECT 1")
            )
        logger.info("database_connection_verified")
        return True
    except Exception as e:
        logger.error("database_connection_failed", error=str(e))
        return False


async def close_database() -> None:
    """Close the database engine and all connections. Called on app shutdown."""
    await engine.dispose()
    logger.info("database_engine_closed")
