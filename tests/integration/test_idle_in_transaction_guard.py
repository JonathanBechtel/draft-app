"""Regression tests for the idle-in-transaction leak guard (#572).

A Summer League ingestion/roster session was left ``idle in transaction`` for
24+ minutes after a read against ``summer_league_source_players``, holding
``players_master`` locks (taken while inserting stub rows) and blocking a
deploy's ``CREATE TABLE ... FK players_master`` for ~20 minutes. The permanent
fix is a role-level ``idle_in_transaction_session_timeout`` (see the
``f3a1c2b4d5e6`` migration) so Postgres reaps any abandoned transaction
automatically.

These tests exercise two guarantees the fix depends on:

1. Postgres terminates a backend that sits idle *inside a transaction* past
   the configured timeout -- the reaper the migration installs.
2. The application's own session context manager (``SessionLocal`` /
   ``session_factory``) never leaks a lingering idle-in-transaction backend
   after a normal read/write, matching the "no leak past a tick" assertion the
   ticket asks for.
"""

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, InterfaceError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

pytestmark = pytest.mark.asyncio


async def test_idle_in_transaction_timeout_reaps_abandoned_session(
    database_url: str,
) -> None:
    """A session idle in a transaction past the timeout is terminated by PG.

    Sets a deliberately tiny session-level timeout (the migration installs the
    same GUC at the role level, 180s), opens a transaction with a read, then
    stays idle past the threshold. The next statement must fail because
    Postgres terminated the backend -- proving the reaper the fix relies on
    actually fires. The server itself stays healthy (a fresh connection works).

    Runs on a dedicated ``NullPool`` engine, never the shared session-scoped
    one: the session-level timeout GUC and the deliberately-terminated backend
    must not leak into the pool the rest of the suite reuses, or they would
    reap other tests' transactions.
    """
    engine = create_async_engine(
        database_url,
        poolclass=NullPool,
        connect_args={
            "prepared_statement_cache_size": 0,
            "statement_cache_size": 0,
        },
    )
    try:
        async with engine.connect() as conn:
            await conn.exec_driver_sql(
                "SET idle_in_transaction_session_timeout = '250ms'"
            )
            # Autobegins a transaction; the connection is now idle-in-transaction.
            await conn.exec_driver_sql("SELECT 1")
            await asyncio.sleep(0.6)
            with pytest.raises((DBAPIError, InterfaceError)):
                await conn.exec_driver_sql("SELECT 1")

        # The server is unharmed: only the offending backend was reaped.
        async with engine.connect() as healthy:
            assert (await healthy.exec_driver_sql("SELECT 1")).scalar() == 1
    finally:
        await engine.dispose()


async def _count_idle_in_transaction(engine: AsyncEngine, app_name: str) -> int:
    """Count backends tagged ``app_name`` that are idle inside a transaction.

    Runs on its own short-lived connection (the engine is NullPool, so this is
    a distinct backend). That backend is ``active`` while its own query runs,
    so the ``state`` filter naturally excludes the observer itself.
    """
    async with engine.connect() as observer:
        return (
            await observer.exec_driver_sql(
                "SELECT count(*) FROM pg_stat_activity "
                f"WHERE application_name = '{app_name}' "
                "AND state = 'idle in transaction'"
            )
        ).scalar()


async def test_session_context_manager_releases_open_transaction(
    database_url: str,
    async_engine: AsyncEngine,
    test_schema: str,
) -> None:
    """Closing a session releases its open transaction -- no idle-in-txn leak.

    Mirrors the leak-prone seam from #572: read
    ``summer_league_source_players`` by ``nba_stats_person_id`` and write a
    row, leaving a transaction open, then let the ``async with`` block close
    the session *without* an explicit commit. The backend must be idle in
    transaction *before* the close and gone *after* -- proving the context
    manager (the same idiom ``SessionLocal`` uses) actually releases it.

    Everything runs on a dedicated ``NullPool`` engine, never the shared
    session-scoped pool: this test mutates connection state (search_path, an
    open transaction) that would otherwise poison pooled connections the rest
    of the suite reuses. ``application_name`` is pinned via ``server_settings``
    so it survives the rollback and stays visible to the observer. The
    ``async_engine`` fixture is depended on only to create the test schema and
    its tables.
    """
    app_name = f"idle_guard_test_{uuid.uuid4().hex[:12]}"
    person_id = f"leak-{uuid.uuid4().hex[:8]}"

    engine = create_async_engine(
        database_url,
        poolclass=NullPool,
        connect_args={
            "prepared_statement_cache_size": 0,
            "statement_cache_size": 0,
            "server_settings": {"application_name": app_name},
        },
    )
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        async with factory() as db:
            await db.execute(text(f'SET search_path TO "{test_schema}"'))
            # The exact query shape from the incident (read by person id)...
            await db.execute(
                text(
                    "SELECT id FROM summer_league_source_players "
                    "WHERE nba_stats_person_id = :pid"
                ),
                {"pid": person_id},
            )
            # ...followed by a write, so a transaction is genuinely open.
            await db.execute(
                text(
                    "INSERT INTO summer_league_source_players "
                    "(nba_stats_person_id, raw_player_name, normalized_name, "
                    " first_seen_year, last_seen_year, created_at, updated_at) "
                    "VALUES (:pid, 'Leak Test', 'leak test', 2026, 2026, "
                    " now(), now())"
                ),
                {"pid": person_id},
            )
            # Sanity: the transaction really is open (idle in transaction) now.
            open_now = await _count_idle_in_transaction(engine, app_name)
            assert open_now >= 1, "expected an open idle-in-transaction backend"
            # Intentionally no commit -- rely on the context manager to release.

        # Session closed; the transaction must be gone (backend released).
        lingering = await _count_idle_in_transaction(engine, app_name)
    finally:
        await engine.dispose()

    assert lingering == 0, (
        f"session left {lingering} backend(s) idle in transaction after close"
    )
