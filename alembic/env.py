"""Alembic environment configuration for DraftGuru."""
import asyncio
import os
import ssl
from logging.config import fileConfig
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlmodel import SQLModel

# Import models so SQLModel metadata is populated.
from app.schemas import players  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Load local .env file when present so local migrations work without manual exports.
env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(env_path, override=False)

DB_URL = os.getenv("DATABASE_URL")
if not DB_URL:
    raise RuntimeError("DATABASE_URL is required for Alembic migrations")


split_result = urlsplit(DB_URL)
query_params = dict(parse_qsl(split_result.query, keep_blank_values=True))
sslmode = query_params.pop("sslmode", None)
query_params.pop("channel_binding", None)

clean_query = urlencode(query_params, doseq=True)
DB_URL = urlunsplit(split_result._replace(query=clean_query)).rstrip("?")

connect_args = {}
if sslmode:
    normalized = sslmode.lower()
    if normalized == "disable":
        connect_args["ssl"] = False
    elif normalized in {"require", "verify-full", "verify-ca", "prefer", "allow"}:
        ssl_context = ssl.create_default_context()
        if normalized == "verify-ca":
            ssl_context.check_hostname = False
        connect_args["ssl"] = ssl_context
    else:
        # Fallback to default SSL context for any unrecognized value
        connect_args["ssl"] = ssl.create_default_context()

# asyncpg doesn't accept a channel_binding kwarg; removing it from the URL is enough.

config.set_main_option("sqlalchemy.url", DB_URL)

target_metadata = SQLModel.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    context.configure(
        url=DB_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    connectable: AsyncEngine = create_async_engine(
        DB_URL,
        poolclass=pool.NullPool,
        future=True,
        connect_args=connect_args,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
