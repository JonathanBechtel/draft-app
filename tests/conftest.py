"""Pytest fixtures aligned with the live Postgres stack."""
import asyncio
import os
from pathlib import Path
from typing import AsyncGenerator

import pytest
import pytest_asyncio

from alembic import command
from alembic.config import Config
from dotenv import load_dotenv
from httpx import AsyncClient, ASGITransport
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _make_alembic_config(database_url: str) -> Config:
    """Return an Alembic config object bound to the provided database."""

    os.environ["DATABASE_URL"] = database_url
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _load_database_url() -> str:
    """Resolve the database URL for tests, enforcing an explicit opt-in."""
    test_db_url = os.getenv("TEST_DATABASE_URL")
    pytest_allow_db = int(os.getenv("PYTEST_ALLOW_DB"))
    if not test_db_url:
        pytest.skip("No TEST_DATABASE_URL or DATABASE_URL is configured for tests.")
    if pytest_allow_db != 1:
        raise RuntimeError(
            "Running integration tests requires setting PYTEST_ALLOW_DB=1 to"
            " confirm the configured database is safe to mutate."
        )
    return test_db_url


@pytest.fixture(scope="session")
def event_loop() -> AsyncGenerator[asyncio.AbstractEventLoop, None]:
    """Provide a dedicated event loop for the async test session."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
def database_url() -> str:
    """Return the Postgres URL the test suite should target."""
    return _load_database_url()


@pytest_asyncio.fixture(scope="session")
async def async_engine(database_url: str) -> AsyncGenerator[AsyncEngine, None]:
    """Yield an async engine bound to the integration-test database."""
    engine = create_async_engine(database_url, echo=False, pool_pre_ping=True)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture()
async def db_session(async_engine: AsyncEngine, database_url: str) -> AsyncGenerator[AsyncSession, None]:
    """Provide a clean transactional session for each test."""
    alembic_config = _make_alembic_config(database_url)
    command.downgrade(alembic_config, "base")
    command.upgrade(alembic_config, "head")

    session_factory = async_sessionmaker(async_engine, expire_on_commit=False, class_=AsyncSession)

    async with session_factory() as session:
        try:
            yield session
        finally:
            await session.rollback()
            command.downgrade(alembic_config, "base")


@pytest_asyncio.fixture()
async def app_client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """Provide an HTTP client with the application wired to the test session."""
    try:
        from app.main import app
    except ValidationError as exc:  # pragma: no cover - guard for misconfigured env
        pytest.skip(f"App configuration failed: {exc}")

    from app.utils.db_async import get_session

    async def _get_session_override() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_session] = _get_session_override
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_session, None)


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """Ensure HTTPX uses asyncio backend during tests."""
    return "asyncio"
