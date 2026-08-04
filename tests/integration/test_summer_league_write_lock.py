"""Integration coverage for Summer League cross-cron writer serialization."""

import asyncio
from time import monotonic

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.services.ingest.write_lock import (
    SummerLeagueWriterLockTimeout,
    acquire_summer_league_writer_lock,
    acquire_summer_league_writer_lock_bounded,
    clear_desk_waiting,
    desk_is_waiting,
    mark_desk_waiting,
    try_acquire_summer_league_writer_lock,
)

pytestmark = pytest.mark.committed_db


async def _set_schema(session: AsyncSession, test_schema: str) -> None:
    await session.execute(text(f'SET search_path TO "{test_schema}"'))
    await session.commit()


async def _pin_schema(session: AsyncSession, test_schema: str) -> None:
    """Pin ``search_path`` on the current transaction's connection.

    The writer lock keys off ``hashtext(current_schema())`` (see
    :mod:`app.services.ingest.write_lock`), so a cross-session
    contention test only actually contends when both sessions resolve the
    *same* ``current_schema()`` on the exact connections that acquire the
    lock. A plain ``SET search_path`` + commit before ``begin()`` (see
    :func:`_set_schema`) sets the path on whatever connection that statement
    ran on, but the pool can hand the following ``begin()`` a different
    connection whose ``search_path`` still defaults to ``public`` -- so the
    two sessions silently key their advisory locks off different schemas and
    never contend (the lock is acquired by both, no timeout, no handoff).
    This only bites on a cold pool; once earlier tests have set the path on
    every pooled connection the flake disappears, which is exactly the kind
    of order-dependent, warm-only pass this pins shut. Issuing ``SET LOCAL``
    as the first statement inside the lock transaction guarantees the path is
    set on the same connection the lock is acquired on.
    """
    await session.execute(text(f'SET LOCAL search_path TO "{test_schema}"'))


@pytest.mark.asyncio
async def test_writer_lock_blocks_a_second_transaction_in_the_same_schema(
    session_factory: async_sessionmaker[AsyncSession],
    test_schema: str,
) -> None:
    """Desk's blocking lock and ingestion's try-lock share one transaction key."""
    async with session_factory() as desk, session_factory() as ingestion:
        await desk.execute(text(f'SET search_path TO "{test_schema}"'))
        await ingestion.execute(text(f'SET search_path TO "{test_schema}"'))
        await desk.commit()
        await ingestion.commit()
        async with desk.begin():
            await _pin_schema(desk, test_schema)
            await acquire_summer_league_writer_lock(desk)
            async with ingestion.begin():
                await _pin_schema(ingestion, test_schema)
                assert await try_acquire_summer_league_writer_lock(ingestion) is False

        async with ingestion.begin():
            await _pin_schema(ingestion, test_schema)
            assert await try_acquire_summer_league_writer_lock(ingestion) is True


@pytest.mark.asyncio
async def test_bounded_acquire_succeeds_immediately_when_lock_is_free(
    session_factory: async_sessionmaker[AsyncSession],
    test_schema: str,
) -> None:
    """An uncontended bounded acquire returns well within its bound."""
    async with session_factory() as db:
        await _set_schema(db, test_schema)
        async with db.begin():
            await _pin_schema(db, test_schema)
            started_at = monotonic()
            await acquire_summer_league_writer_lock_bounded(db, max_wait_seconds=5.0)
            elapsed = monotonic() - started_at
        assert elapsed < 1.0


@pytest.mark.asyncio
async def test_bounded_acquire_raises_timeout_when_lock_stays_held(
    session_factory: async_sessionmaker[AsyncSession],
    test_schema: str,
) -> None:
    """A second session's bounded acquire raises (not hangs) once the deadline passes."""
    async with session_factory() as holder, session_factory() as waiter:
        await _set_schema(holder, test_schema)
        await _set_schema(waiter, test_schema)
        async with holder.begin():
            await _pin_schema(holder, test_schema)
            await acquire_summer_league_writer_lock(holder)

            async with waiter.begin():
                await _pin_schema(waiter, test_schema)
                max_wait_seconds = 1.0
                started_at = monotonic()
                with pytest.raises(SummerLeagueWriterLockTimeout):
                    await acquire_summer_league_writer_lock_bounded(
                        waiter, max_wait_seconds=max_wait_seconds
                    )
                elapsed = monotonic() - started_at
                # Bounded, not hanging: returns close to (never wildly past)
                # the configured deadline -- generous upper slack accounts for
                # real network round trips to the test database plus xdist
                # worker contention, not just local scheduling jitter.
                assert max_wait_seconds <= elapsed < max_wait_seconds + 5.0

                # The priority-intent signal raised during the wait is always
                # cleared on the way out -- a timed-out wait must not leave a
                # stale "Desk is waiting" marker behind for a later probe.
                assert await desk_is_waiting(waiter) is False


@pytest.mark.asyncio
async def test_bounded_acquire_succeeds_once_holder_releases_before_deadline(
    session_factory: async_sessionmaker[AsyncSession],
    test_schema: str,
) -> None:
    """A bounded acquire keeps retrying and wins the lock once it's freed mid-wait."""
    async with session_factory() as holder, session_factory() as waiter:
        await _set_schema(holder, test_schema)
        await _set_schema(waiter, test_schema)

        holder_ready = asyncio.Event()

        async def _hold_then_release() -> None:
            async with holder.begin():
                await _pin_schema(holder, test_schema)
                await acquire_summer_league_writer_lock(holder)
                holder_ready.set()
                await asyncio.sleep(0.3)
            # Transaction end (commit) releases the transaction-scoped lock.

        async def _wait_for_lock() -> None:
            # Deterministically start waiting only once the holder genuinely
            # has the lock, so this proves the retry loop actually observes
            # contention (and later success), not a lucky race.
            await holder_ready.wait()
            async with waiter.begin():
                await _pin_schema(waiter, test_schema)
                await acquire_summer_league_writer_lock_bounded(
                    waiter, max_wait_seconds=5.0
                )

        await asyncio.gather(_hold_then_release(), _wait_for_lock())


@pytest.mark.asyncio
async def test_desk_waiting_signal_is_visible_cross_session_until_cleared(
    session_factory: async_sessionmaker[AsyncSession],
    test_schema: str,
) -> None:
    """``mark_desk_waiting``/``desk_is_waiting``/``clear_desk_waiting`` round-trip.

    A lower-priority writer (a separate session/connection, e.g. full
    ingestion) must see the Desk's priority-intent signal turn on the moment
    another session raises it and turn back off the moment it's cleared --
    proving this is a real cross-session Postgres signal, not just an
    in-process flag local to the session that raised it.
    """
    async with session_factory() as desk, session_factory() as ingestion:
        await _set_schema(desk, test_schema)
        await _set_schema(ingestion, test_schema)

        async with ingestion.begin():
            await _pin_schema(ingestion, test_schema)
            assert await desk_is_waiting(ingestion) is False

        async with desk.begin():
            await _pin_schema(desk, test_schema)
            assert await mark_desk_waiting(desk) is True

            async with ingestion.begin():
                await _pin_schema(ingestion, test_schema)
                assert await desk_is_waiting(ingestion) is True

            await clear_desk_waiting(desk)

            async with ingestion.begin():
                await _pin_schema(ingestion, test_schema)
                assert await desk_is_waiting(ingestion) is False
