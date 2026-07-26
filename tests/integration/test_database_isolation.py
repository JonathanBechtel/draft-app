"""Integration-test harness contracts for database isolation modes."""

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from app.schemas.players_master import PlayerMaster
from tests.integration.conftest import make_player


@pytest.mark.asyncio
async def test_default_sessions_join_a_connection_owned_transaction(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Normal tests use savepoints inside a connection-owned rollback boundary."""
    assert isinstance(session_factory.kw["bind"], AsyncConnection)
    assert session_factory.kw["join_transaction_mode"] == "create_savepoint"


@pytest.mark.asyncio
async def test_default_commit_is_visible_to_fresh_sessions_but_not_other_connections(
    session_factory: async_sessionmaker[AsyncSession],
    async_engine: AsyncEngine,
    test_schema: str,
) -> None:
    """A session commit releases a savepoint without escaping test rollback."""
    async with session_factory() as setup_session:
        async with setup_session.begin():
            setup_session.add(make_player("Rollback", "Probe"))

    async with session_factory() as request_session:
        visible_count = (
            await request_session.execute(select(func.count()).select_from(PlayerMaster))
        ).scalar_one()
        await request_session.rollback()
    assert visible_count == 1

    async with async_engine.connect() as independent_connection:
        durable_count = (
            await independent_connection.execute(
                text(f'SELECT count(*) FROM "{test_schema}".players_master')
            )
        ).scalar_one()
    assert durable_count == 0


@pytest.mark.asyncio
async def test_http_clients_use_independent_engine_connections(
    app_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """HTTP tests retain production-like commits across separate requests."""
    _ = app_client
    assert isinstance(session_factory.kw["bind"], AsyncEngine)


@pytest.mark.asyncio
@pytest.mark.committed_db
async def test_committed_db_sessions_keep_independent_engine_connections(
    session_factory: async_sessionmaker[AsyncSession],
    async_engine: AsyncEngine,
    test_schema: str,
) -> None:
    """Concurrency and durability tests can opt into real committed connections."""
    assert isinstance(session_factory.kw["bind"], AsyncEngine)

    async with session_factory() as session:
        async with session.begin():
            await session.execute(text(f'SET LOCAL search_path TO "{test_schema}"'))
            session.add(make_player("Committed", "Probe"))

    async with async_engine.connect() as independent_connection:
        durable_count = (
            await independent_connection.execute(
                text(f'SELECT count(*) FROM "{test_schema}".players_master')
            )
        ).scalar_one()
    assert durable_count == 1
