"""
Connection to the database using SQLAlchemy's async capabilities.
"""
from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlmodel import SQLModel
from sqlalchemy.engine import make_url


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
            return str(u)
        # Otherwise, normalize bare postgres/postgresql to asyncpg for async engines.
        if driver in ("postgres", "postgresql"):
            u = u.set(drivername="postgresql+asyncpg")
        return str(u)
    except Exception:
        # Fallback string-level normalization for odd/partial URLs
        if url.startswith("postgresql+asyncpg://"):
            return url
        if url.startswith("postgresql://"):
            return "postgresql+asyncpg://" + url.split("://", 1)[1]
        if url.startswith("postgres://"):
            return "postgresql+asyncpg://" + url.split("://", 1)[1]
        return url

print("DATABASE_URL (raw):", settings.database_url)
logger = settings.log_level
# DATABASE_URL = _normalize_db_url(settings.database_url)

DATABASE_URL = settings.database_url

engine = create_async_engine(DATABASE_URL, echo=settings.sql_echo, pool_pre_ping=True)
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
