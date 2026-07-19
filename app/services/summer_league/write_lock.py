"""Transaction-scoped serialization for Summer League projection writers."""

import asyncio
from time import monotonic

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# "SLDE" as a signed 32-bit advisory-lock namespace. The first lock key is
# PostgreSQL's hash of current_schema(): production writers share ``public``,
# while pytest-xdist's isolated schemas do not unnecessarily serialize.
_SUMMER_LEAGUE_WRITER_LOCK_KEY = 0x534C4445

# "SLDW" ("SL Desk Waiting") -- a distinct advisory-lock key from the writer
# lock above, held session-scoped (not transaction-scoped) for the duration
# of a bounded wait. It is a pure priority-intent signal, never a mutual
# exclusion mechanism of its own: a lower-priority writer probes it with a
# non-blocking `pg_try_advisory_lock`/`pg_advisory_unlock` pair
# (:func:`desk_is_waiting`) before its own reacquisition attempt, rather than
# racing the Desk for the writer lock immediately after releasing it.
_DESK_WAITING_SIGNAL_KEY = 0x534C4457

# Interval between bounded-wait retry attempts. Short enough to keep the
# Desk's observed wait close to its true deadline, long enough to avoid
# hammering Postgres with advisory-lock probes.
_BOUNDED_WAIT_POLL_INTERVAL_SECONDS = 0.1


class SummerLeagueWriterLockTimeout(RuntimeError):
    """Raised when a bounded lock wait exceeds its deadline."""


async def acquire_summer_league_writer_lock(db: AsyncSession) -> None:
    """Wait for the shared Summer League writer lock for this transaction."""
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(current_schema()), :lock_key)"),
        {"lock_key": _SUMMER_LEAGUE_WRITER_LOCK_KEY},
    )


async def try_acquire_summer_league_writer_lock(db: AsyncSession) -> bool:
    """Attempt the shared writer lock without delaying lower-priority ingestion."""
    result = await db.execute(
        text("SELECT pg_try_advisory_xact_lock(hashtext(current_schema()), :lock_key)"),
        {"lock_key": _SUMMER_LEAGUE_WRITER_LOCK_KEY},
    )
    return bool(result.scalar_one())


async def acquire_summer_league_writer_lock_bounded(
    db: AsyncSession, *, max_wait_seconds: float
) -> None:
    """Wait up to ``max_wait_seconds`` for the writer lock; raise on timeout.

    Unlike :func:`acquire_summer_league_writer_lock` (an unbounded blocking
    ``pg_advisory_xact_lock`` wait), this polls the same non-blocking
    ``pg_try_advisory_xact_lock`` primitive :func:`try_acquire_summer_league_writer_lock`
    uses, on a short interval, bounded by a wall-clock deadline -- so a
    higher-priority caller (the Summer League Desk tick) can never block past
    an explicit maximum no matter how long a lower-priority writer holds the
    lock.

    While waiting, this raises the priority-intent signal
    (:func:`mark_desk_waiting`) so a lower-priority writer checking
    :func:`desk_is_waiting` before its own reacquisition attempt can back off
    rather than immediately out-racing this call for the lock; the signal is
    always cleared (:func:`clear_desk_waiting`) before this function returns
    or raises, including on the uncontended immediate-acquire path.

    Preserves the transaction-scoped-lock contract the rest of this module
    relies on: on success the lock is held exactly as
    ``pg_advisory_xact_lock`` would leave it (auto-released on this
    transaction's commit/rollback); on timeout no lock is held at all.

    Args:
        db: Active database session (caller controls the transaction).
        max_wait_seconds: Maximum wall-clock time, in seconds, to wait for
            the lock before giving up.

    Raises:
        SummerLeagueWriterLockTimeout: The lock was not acquired before the
            deadline.
    """
    deadline = monotonic() + max_wait_seconds
    marked = await mark_desk_waiting(db)
    try:
        while True:
            if await try_acquire_summer_league_writer_lock(db):
                return
            if monotonic() >= deadline:
                raise SummerLeagueWriterLockTimeout(
                    "Timed out after "
                    f"{max_wait_seconds:.1f}s waiting for the Summer League "
                    "writer lock"
                )
            await asyncio.sleep(_BOUNDED_WAIT_POLL_INTERVAL_SECONDS)
    finally:
        if marked:
            await clear_desk_waiting(db)


async def mark_desk_waiting(db: AsyncSession) -> bool:
    """Raise the priority-intent signal: the Desk tick is waiting for the writer lock.

    Session-scoped (``pg_try_advisory_lock``), not transaction-scoped -- it
    must outlive the many short-lived probe attempts
    :func:`acquire_summer_league_writer_lock_bounded` makes against the
    transaction-scoped writer lock while it waits, and it is explicitly
    cleared by :func:`clear_desk_waiting` rather than released by a
    commit/rollback.

    Args:
        db: Active database session.

    Returns:
        Whether the marker was actually acquired. Expected to be ``True`` in
        the normal single-Desk-tick topology; a second concurrent Desk tick
        (which should never happen operationally -- the Desk is a single
        scheduled cron) would see ``False`` here and skip clearing a marker
        it doesn't own.
    """
    result = await db.execute(
        text("SELECT pg_try_advisory_lock(hashtext(current_schema()), :lock_key)"),
        {"lock_key": _DESK_WAITING_SIGNAL_KEY},
    )
    return bool(result.scalar_one())


async def clear_desk_waiting(db: AsyncSession) -> None:
    """Clear the priority-intent signal raised by :func:`mark_desk_waiting`."""
    await db.execute(
        text("SELECT pg_advisory_unlock(hashtext(current_schema()), :lock_key)"),
        {"lock_key": _DESK_WAITING_SIGNAL_KEY},
    )


async def desk_is_waiting(db: AsyncSession) -> bool:
    """Whether a Desk tick currently holds the :func:`mark_desk_waiting` signal.

    A lower-priority writer (full ingestion) calls this before its own
    ``try_acquire_summer_league_writer_lock`` reacquisition attempt to back
    off cooperatively when the Desk is actively waiting, rather than
    immediately out-racing it for the lock on every batch boundary.

    Implemented as a non-blocking probe of the same session-scoped marker
    :func:`mark_desk_waiting` holds: if this call can itself acquire the
    marker lock, nobody was holding it (the Desk is not waiting) -- release
    it immediately and return ``False``. If it cannot, the Desk currently
    holds it -- return ``True`` without touching lock state.

    Args:
        db: Active database session.

    Returns:
        Whether a Desk tick is currently waiting for the writer lock.
    """
    result = await db.execute(
        text("SELECT pg_try_advisory_lock(hashtext(current_schema()), :lock_key)"),
        {"lock_key": _DESK_WAITING_SIGNAL_KEY},
    )
    acquired = bool(result.scalar_one())
    if not acquired:
        return True
    await db.execute(
        text("SELECT pg_advisory_unlock(hashtext(current_schema()), :lock_key)"),
        {"lock_key": _DESK_WAITING_SIGNAL_KEY},
    )
    return False
