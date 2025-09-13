"""
Connection to the database using SQLAlchemy's async capabilities.
"""
from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlmodel import SQLModel

from app.config import settings

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
    from app.models import players  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

async def dispose_engine() -> None:
    """Dispose of the async engine and its connection pool."""
    await engine.dispose()
