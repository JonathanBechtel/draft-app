"""Pytest fixtures aligned with the live Postgres stack."""

import asyncio
import os
import secrets
from typing import AsyncGenerator

import pytest
import pytest_asyncio

from httpx import AsyncClient, ASGITransport
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlmodel import SQLModel
from dotenv import load_dotenv

load_dotenv()


def _quote_ident(identifier: str) -> str:
    """Quote a Postgres identifier (schema/table/etc)."""
    return '"' + identifier.replace('"', '""') + '"'


def _load_database_url() -> str:
    """Resolve the database URL for tests, enforcing an explicit opt-in."""
    test_db_url = os.getenv("TEST_DATABASE_URL")
    app_db_url = os.getenv("DATABASE_URL")
    pytest_allow_db = int(os.getenv("PYTEST_ALLOW_DB", "0"))
    if not test_db_url:
        pytest.skip("No TEST_DATABASE_URL or DATABASE_URL is configured for tests.")
    if pytest_allow_db != 1:
        raise RuntimeError(
            "Running integration tests requires setting PYTEST_ALLOW_DB=1 to"
            " confirm the configured database is safe to mutate."
        )

    # Extra guardrail: prevent accidentally running integration tests against the
    # same DB as the app (even though we isolate via schema).
    if (
        app_db_url
        and int(os.getenv("PYTEST_ALLOW_TEST_DB_EQUALS_DATABASE_URL", "0")) != 1
    ):
        try:
            test_url = make_url(test_db_url)
            app_url = make_url(app_db_url)
            if (
                test_url.drivername,
                test_url.host,
                test_url.port,
                test_url.database,
            ) == (
                app_url.drivername,
                app_url.host,
                app_url.port,
                app_url.database,
            ):
                pytest.skip(
                    "Refusing to run integration tests against DATABASE_URL; set a"
                    " separate TEST_DATABASE_URL (or override with"
                    " PYTEST_ALLOW_TEST_DB_EQUALS_DATABASE_URL=1)."
                )
        except Exception:
            # If URL parsing fails, fall back to requiring explicit TEST_DATABASE_URL.
            pass

    # mypy: test_db_url is str after the guard above
    return test_db_url  # type: ignore[return-value]


@pytest.fixture(scope="session")
def event_loop():
    """Provide a dedicated event loop for the async test session."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
def database_url() -> str:
    """Return the Postgres URL the test suite should target."""
    return _load_database_url()


@pytest.fixture(scope="session")
def test_schema(database_url: str) -> str:
    """Return a unique schema name to isolate integration tests within a database."""
    try:
        url = make_url(database_url)
        host = url.host or "local"
        dbname = url.database or "db"
        prefix = f"pytest_{host}_{dbname}"
    except Exception:
        prefix = "pytest"
    safe_prefix = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in prefix)[
        :40
    ]
    return f"{safe_prefix}_{secrets.token_hex(8)}"


@pytest_asyncio.fixture(scope="session")
async def async_engine(
    database_url: str, test_schema: str
) -> AsyncGenerator[AsyncEngine, None]:
    """Yield an async engine bound to the integration-test database."""
    # Ensure SQLModel metadata is populated before creating tables.
    from app.schemas import positions  # noqa: F401
    from app.schemas import player_status  # noqa: F401
    from app.schemas import combine_anthro  # noqa: F401
    from app.schemas import combine_agility  # noqa: F401
    from app.schemas import combine_shooting  # noqa: F401
    from app.schemas import metrics  # noqa: F401
    from app.schemas import player_aliases  # noqa: F401
    from app.schemas import player_bio_snapshots  # noqa: F401
    from app.schemas import player_external_ids  # noqa: F401
    from app.schemas import image_snapshots  # noqa: F401
    from app.schemas import players_master  # noqa: F401
    from app.schemas import seasons  # noqa: F401
    from app.schemas import news_sources  # noqa: F401
    from app.schemas import news_items  # noqa: F401
    from app.schemas import auth  # noqa: F401

    connect_args = {
        # Disable prepared statement caching to avoid type OID/cache issues after DDL.
        "prepared_statement_cache_size": 0,
        "statement_cache_size": 0,
    }
    engine = create_async_engine(database_url, echo=False, pool_pre_ping=True, connect_args=connect_args)
    async with engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{test_schema}"'))
        # Use a schema-only search_path so tests never fall back to `public`.
        # This prevents accidental cross-test contamination if `public` already
        # contains tables with the same names (e.g., from previous runs).
        await conn.execute(text(f'SET search_path TO "{test_schema}"'))
        await conn.run_sync(SQLModel.metadata.create_all)
    try:
        yield engine
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{test_schema}" CASCADE'))
        await engine.dispose()


@pytest_asyncio.fixture(scope="session")
def session_factory(async_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Return a sessionmaker bound to the integration test engine."""
    return async_sessionmaker(bind=async_engine, expire_on_commit=False, class_=AsyncSession)


@pytest_asyncio.fixture(scope="session")
async def truncate_statement(async_engine: AsyncEngine, test_schema: str) -> str:
    """Return a TRUNCATE statement that resets all tables in the test schema."""
    async with async_engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT tablename FROM pg_tables WHERE schemaname = :schema ORDER BY tablename"
            ),
            {"schema": test_schema},
        )
        table_names = [row[0] for row in result.fetchall()]

    if not table_names:
        raise RuntimeError(
            "No tables were found in the integration-test schema. This likely means"
            " the test schema was not applied during table creation (or the database"
            " has conflicting tables in `public` that prevented create_all from"
            " creating schema-local tables)."
        )

    table_refs = ", ".join(
        f"{_quote_ident(test_schema)}.{_quote_ident(table_name)}"
        for table_name in table_names
    )
    return f"TRUNCATE TABLE {table_refs} RESTART IDENTITY CASCADE"


@pytest_asyncio.fixture(autouse=True)
async def truncate_tables(async_engine: AsyncEngine, truncate_statement: str) -> None:
    """Reset all data between integration tests (prod-like isolation)."""
    if not truncate_statement:
        return
    async with async_engine.begin() as conn:
        await conn.execute(text(truncate_statement))


@pytest_asyncio.fixture()
async def db_session(
    session_factory: async_sessionmaker[AsyncSession],
    truncate_tables: None,
    test_schema: str,
) -> AsyncGenerator[AsyncSession, None]:
    """Provide a DB session for test setup/verification."""
    _ = truncate_tables
    async with session_factory() as session:
        await session.execute(text(f'SET search_path TO "{test_schema}"'))
        await session.commit()
        yield session


@pytest_asyncio.fixture()
async def app_client(
    session_factory: async_sessionmaker[AsyncSession],
    truncate_tables: None,
    test_schema: str,
) -> AsyncGenerator[AsyncClient, None]:
    """Provide an HTTP client with the app wired to fresh per-request sessions."""
    _ = truncate_tables
    try:
        from app.main import app
    except ValidationError as exc:  # pragma: no cover - guard for misconfigured env
        pytest.skip(f"App configuration failed: {exc}")

    from app.utils.db_async import get_session

    async def _get_session_override() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            await session.execute(text(f'SET search_path TO "{test_schema}"'))
            await session.commit()
            yield session

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
