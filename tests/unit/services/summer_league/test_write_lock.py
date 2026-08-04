"""Unit tests for the pure composition logic in write_lock.py.

Real advisory-lock contention (``desk_is_waiting``/``mark_desk_waiting``/
``clear_desk_waiting``/``try_acquire_summer_league_writer_lock``/
``acquire_summer_league_writer_lock_bounded``) requires a real Postgres
session and is covered by ``tests/integration/test_summer_league_write_lock.py``.
This file only covers the two higher-level composition helpers'
orchestration logic -- backoff-before-attempt and timing-into-step-fields --
with the underlying primitives monkeypatched, since that logic is pure and
does not itself touch the database.
"""

from __future__ import annotations

import pytest

from app.services.ingest import write_lock


@pytest.mark.asyncio
async def test_try_acquire_writer_lock_yielding_backs_off_when_desk_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A waiting Desk tick triggers a bounded sleep before the lock attempt."""
    sleep_calls: list[float] = []

    async def _waiting(_db: object) -> bool:
        return True

    async def _fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    async def _available(_db: object) -> bool:
        return True

    monkeypatch.setattr(write_lock, "desk_is_waiting", _waiting)
    monkeypatch.setattr(write_lock.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(
        write_lock, "try_acquire_summer_league_writer_lock", _available
    )

    acquired = await write_lock.try_acquire_summer_league_writer_lock_yielding(
        object()  # type: ignore[arg-type]
    )

    assert acquired is True
    assert sleep_calls == [write_lock.LOWER_PRIORITY_BACKOFF_SECONDS]


@pytest.mark.asyncio
async def test_try_acquire_writer_lock_yielding_skips_backoff_when_not_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No sleep is issued when the Desk isn't currently waiting."""
    sleep_calls: list[float] = []

    async def _not_waiting(_db: object) -> bool:
        return False

    async def _fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    async def _unavailable(_db: object) -> bool:
        return False

    monkeypatch.setattr(write_lock, "desk_is_waiting", _not_waiting)
    monkeypatch.setattr(write_lock.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(
        write_lock, "try_acquire_summer_league_writer_lock", _unavailable
    )

    acquired = await write_lock.try_acquire_summer_league_writer_lock_yielding(
        object()  # type: ignore[arg-type]
    )

    assert acquired is False
    assert sleep_calls == []


@pytest.mark.asyncio
async def test_try_acquire_writer_lock_yielding_times_attempt_into_step_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lock attempt's duration is recorded under a distinct field."""

    async def _not_waiting(_db: object) -> bool:
        return False

    async def _available(_db: object) -> bool:
        return True

    monkeypatch.setattr(write_lock, "desk_is_waiting", _not_waiting)
    monkeypatch.setattr(
        write_lock, "try_acquire_summer_league_writer_lock", _available
    )

    step_fields: dict[str, object] = {}
    acquired = await write_lock.try_acquire_summer_league_writer_lock_yielding(
        object(),  # type: ignore[arg-type]
        step_fields,
    )

    assert acquired is True
    assert "writer_lock_wait_ms" in step_fields
    assert isinstance(step_fields["writer_lock_wait_ms"], float)


@pytest.mark.asyncio
async def test_acquire_writer_lock_bounded_timed_times_into_step_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bounded acquire's wait is recorded under a distinct field."""

    async def _bounded(_db: object, *, max_wait_seconds: float) -> None:
        return None

    monkeypatch.setattr(
        write_lock, "acquire_summer_league_writer_lock_bounded", _bounded
    )

    step_fields: dict[str, object] = {}
    await write_lock.acquire_summer_league_writer_lock_bounded_timed(
        object(),  # type: ignore[arg-type]
        max_wait_seconds=5.0,
        step_fields=step_fields,
    )

    assert "writer_lock_wait_ms" in step_fields
    assert isinstance(step_fields["writer_lock_wait_ms"], float)


@pytest.mark.asyncio
async def test_acquire_writer_lock_bounded_timed_records_field_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wait is still recorded when the bounded acquire times out."""

    async def _bounded(_db: object, *, max_wait_seconds: float) -> None:
        raise write_lock.SummerLeagueWriterLockTimeout("timed out")

    monkeypatch.setattr(
        write_lock, "acquire_summer_league_writer_lock_bounded", _bounded
    )

    step_fields: dict[str, object] = {}
    with pytest.raises(write_lock.SummerLeagueWriterLockTimeout):
        await write_lock.acquire_summer_league_writer_lock_bounded_timed(
            object(),  # type: ignore[arg-type]
            max_wait_seconds=5.0,
            step_fields=step_fields,
        )

    assert "writer_lock_wait_ms" in step_fields
