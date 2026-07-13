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
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


async def test_idle_in_transaction_timeout_reaps_abandoned_session(
    async_engine: AsyncEngine,
) -> None:
    """A session idle in a transaction past the timeout is terminated by PG.

    Sets a deliberately tiny session-level timeout (the migration installs the
    same GUC at the role level, 180s), opens a transaction with a read, then
    stays idle past the threshold. The next statement must fail because
    Postgres terminated the backend -- proving the reaper the fix relies on
    actually fires. The server itself stays healthy (a fresh connection works).
    """
    async with async_engine.connect() as conn:
        await conn.exec_driver_sql("SET idle_in_transaction_session_timeout = '250ms'")
        # Autobegins a transaction; the connection is now idle-in-transaction.
        await conn.exec_driver_sql("SELECT 1")
        await asyncio.sleep(0.6)
        with pytest.raises((DBAPIError, InterfaceError)):
            await conn.exec_driver_sql("SELECT 1")

    # The server is unharmed: only the offending backend was reaped.
    async with async_engine.connect() as healthy:
        assert (await healthy.exec_driver_sql("SELECT 1")).scalar() == 1


async def test_session_context_manager_leaves_no_idle_in_transaction(
    async_engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A normal read/write session leaves no idle-in-transaction backend.

    Mirrors the leak-prone seam from #572: read
    ``summer_league_source_players`` by ``nba_stats_person_id`` and write a
    row, then let the ``async with`` block close the session *without* an
    explicit commit. Afterwards, no backend tagged with this test's unique
    application_name may remain ``idle in transaction`` -- the context manager
    must have released the transaction on exit.
    """
    app_name = f"idle_guard_test_{uuid.uuid4().hex[:12]}"
    person_id = f"leak-{uuid.uuid4().hex[:8]}"

    async with session_factory() as db:
        await db.execute(text(f"SET application_name = '{app_name}'"))
        # The exact query shape from the incident (read by person id)...
        await db.execute(
            text(
                "SELECT id FROM summer_league_source_players "
                "WHERE nba_stats_person_id = :pid"
            ),
            {"pid": person_id},
        )
        # ...followed by a write, so a transaction is genuinely open on exit.
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
        # Intentionally no commit -- rely on the context manager to release.

    # Observe from a separate backend so we never see our own transaction.
    async with async_engine.connect() as observer:
        lingering = (
            await observer.exec_driver_sql(
                "SELECT count(*) FROM pg_stat_activity "
                f"WHERE application_name = '{app_name}' "
                "AND state = 'idle in transaction'"
            )
        ).scalar()

    assert lingering == 0, (
        f"session left {lingering} backend(s) idle in transaction after close"
    )
