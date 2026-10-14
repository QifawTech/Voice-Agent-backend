"""
Alembic Environment Configuration — Async Mode

This env.py is configured for async migrations using asyncpg.
It loads the database URL from the application's .env file
and imports the SQLAlchemy Base metadata for autogeneration.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

# Load .env file so DATABASE_URL is available
from dotenv import load_dotenv
import os

load_dotenv()

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Override sqlalchemy.url with value from environment
config.set_main_option("sqlalchemy.url", os.getenv("DATABASE_URL", ""))

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Import the Base metadata for autogenerate support
# This will include all models that inherit from Base
from app.database import Base

# Import all models here so Alembic can detect them
from app.models.tenant import Tenant  # noqa: F401
from app.models.admin_user import AdminUser  # noqa: F401
from app.models.conversation_log import ConversationLog  # noqa: F401
from app.models.order_event import OrderEvent  # noqa: F401
from app.models.daily_analytics import DailyAnalytics  # noqa: F401

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """
    Run migrations in 'offline' mode.

    This generates SQL scripts without connecting to the database.
    Useful for reviewing migration SQL before applying.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    """Run migrations with a live database connection."""
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """
    Run migrations in 'online' mode using async engine.

    Creates an async engine from the configuration and runs
    migrations within a connection context.
    """
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Entry point for online migrations — delegates to async runner."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
