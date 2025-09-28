"""Async SQLAlchemy engine and session helpers."""

import ssl
from typing import Any, AsyncGenerator, Dict, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from app.config import settings

def _normalize_db_url(url: str) -> str:
    """Ensure an async-capable PostgreSQL driver is selected when using Postgres.

    If the URL is plain "postgresql://..." or the alias "postgres://...",
    switch to the asyncpg driver via "postgresql+asyncpg://...".
    """
    try:
        u = make_url(url)
        driver = (u.drivername or "").lower()
        # If an explicit driver is present (e.g., postgresql+psycopg), respect it.
        if "+" in driver:
            return u.render_as_string(hide_password=False)
        # Otherwise, normalize bare postgres/postgresql to asyncpg for async engines.
        if driver in ("postgres", "postgresql"):
            u = u.set(drivername="postgresql+asyncpg")
        return u.render_as_string(hide_password=False)
    except Exception:
        # Fallback string-level normalization for odd/partial URLs
        if url.startswith("postgresql+asyncpg://"):
            return url
        if url.startswith("postgresql://"):
            return "postgresql+asyncpg://" + url.split("://", 1)[1]
        if url.startswith("postgres://"):
            return "postgresql+asyncpg://" + url.split("://", 1)[1]
        return url


def _prepare_asyncpg_connection(url: str) -> Tuple[str, Dict[str, Any]]:
    """Strip unsupported query args and derive asyncpg connect kwargs."""

    normalized_url = _normalize_db_url(url)
    split = urlsplit(normalized_url)
    query_pairs = parse_qsl(split.query, keep_blank_values=True)

    sslmode = None
    filtered_pairs = []
    for key, value in query_pairs:
        if key == "sslmode":
            sslmode = value
            continue
        if key == "channel_binding":
            # asyncpg does not accept this kwarg; drop it.
            continue
        filtered_pairs.append((key, value))

    cleaned_query = urlencode(filtered_pairs, doseq=True)
    cleaned_url = urlunsplit(split._replace(query=cleaned_query)).rstrip("?")

    connect_args: Dict[str, Any] = {}
    if sslmode:
        mode = sslmode.lower()
        if mode == "disable":
            connect_args["ssl"] = False
        elif mode in {"allow", "prefer"}:
            # asyncpg defaults to negotiating TLS when the server requires it, so we
            # simply avoid forcing a context and let the driver perform the fallback.
            pass
        elif mode == "require":
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            connect_args["ssl"] = ssl_context
        elif mode == "verify-ca":
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            connect_args["ssl"] = ssl_context
        elif mode == "verify-full":
            connect_args["ssl"] = ssl.create_default_context()
        else:
            # Unknown value—fall back to a secure default.
            connect_args["ssl"] = ssl.create_default_context()

    return cleaned_url, connect_args


DATABASE_URL, CONNECT_ARGS = _prepare_asyncpg_connection(settings.database_url)

engine = create_async_engine(
    DATABASE_URL,
    echo=settings.sql_echo,
    pool_pre_ping=True,
    connect_args=CONNECT_ARGS,
)
SessionLocal = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)

async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield an async database session."""
    async with SessionLocal() as session:
        yield session

async def init_db():
    """Initialize the database (create tables)."""
    # Ensure models are imported so metadata is fully populated
    # Import locally to avoid circular imports at module import time
    from app.schemas import players  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

async def dispose_engine() -> None:
    """Dispose of the async engine and its connection pool."""
    await engine.dispose()

def describe_database_url(url: str) -> str:
    """Return a sanitized, human-readable description of the DB URL for logging.

    Example: "postgresql+asyncpg://user@host:5432/dbname"
    Passwords are never included.
    """
    try:
        u = make_url(url)
        auth = u.username or "?"
        host = u.host or "?"
        port = f":{u.port}" if u.port else ""
        db = u.database or "?"
        return f"{u.drivername}://{auth}@{host}{port}/{db}"
    except Exception:
        # On parse failure, do not log the raw URL; hint only
        return "<unparseable database URL>"
