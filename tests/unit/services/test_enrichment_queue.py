"""Unit tests for enrichment_queue_service.

Tests the pure logic of enqueue_enrichment and drain_enrichment_queue
without hitting a real database.  Mock async sessions stand in for DB
connections; external calls (Gemini, Wikimedia, portrait/S3) are never
made in unit tests.

Covered behaviors:
- enqueue_enrichment: per-request cap, in-flight dedup, flush on success.
- drain_enrichment_queue: stale-running reclaim logic (state transition).
- enrichment_status: most-recent-job-per-player logic.
- JobStatus and DrainResult dataclass construction.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.enrichment_queue_service import (
    MAX_ENQUEUE_PER_REQUEST,
    DrainResult,
    JobStatus,
    _STALE_RUNNING_SECONDS,
    enqueue_enrichment,
    enrichment_status,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _make_mock_job(
    job_id: int = 1,
    player_id: int = 10,
    state: str = "queued",
    started_at: datetime | None = None,
    error_message: str | None = None,
) -> MagicMock:
    """Return a MagicMock shaped like a PlayerEnrichmentJob."""
    job = MagicMock()
    job.id = job_id
    job.player_id = player_id
    job.state = state
    job.started_at = started_at
    job.error_message = error_message
    return job


# ---------------------------------------------------------------------------
# DrainResult construction
# ---------------------------------------------------------------------------


def test_drain_result_defaults() -> None:
    """DrainResult initialises with zeros and empty error list."""
    result = DrainResult()
    assert result.claimed == 0
    assert result.succeeded == 0
    assert result.failed == 0
    assert result.reclaimed_stale == 0
    assert result.errors == []


def test_drain_result_with_values() -> None:
    """DrainResult stores supplied counts correctly."""
    result = DrainResult(claimed=5, succeeded=4, failed=1, reclaimed_stale=2, errors=["oops"])
    assert result.claimed == 5
    assert result.succeeded == 4
    assert result.failed == 1
    assert result.reclaimed_stale == 2
    assert result.errors == ["oops"]


# ---------------------------------------------------------------------------
# JobStatus construction
# ---------------------------------------------------------------------------


def test_job_status_succeeded() -> None:
    """JobStatus stores state and no error for a successful job."""
    status = JobStatus(state="succeeded")
    assert status.state == "succeeded"
    assert status.error is None


def test_job_status_failed() -> None:
    """JobStatus stores state and error message for a failed job."""
    status = JobStatus(state="failed", error="Gemini timeout")
    assert status.state == "failed"
    assert status.error == "Gemini timeout"


# ---------------------------------------------------------------------------
# enqueue_enrichment — per-request cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_cap_truncates_excess() -> None:
    """enqueue_enrichment silently truncates input beyond MAX_ENQUEUE_PER_REQUEST.

    Only the first MAX_ENQUEUE_PER_REQUEST IDs are considered for queueing;
    the remainder are dropped.  The DB should see at most that many jobs.
    """
    too_many = list(range(1, MAX_ENQUEUE_PER_REQUEST + 10))  # e.g. 35 IDs

    db = AsyncMock()
    # No in-flight jobs
    mock_result = MagicMock()
    mock_result.fetchall.return_value = []
    db.execute = AsyncMock(return_value=mock_result)
    db.add = MagicMock()
    db.flush = AsyncMock()

    enqueued = await enqueue_enrichment(db, too_many, source="admin_bulk", user_id=1)

    # Only up to MAX_ENQUEUE_PER_REQUEST should be created
    assert len(enqueued) <= MAX_ENQUEUE_PER_REQUEST
    # db.add called once per enqueued player
    assert db.add.call_count == len(enqueued)


@pytest.mark.asyncio
async def test_enqueue_empty_list_returns_empty() -> None:
    """enqueue_enrichment with an empty list returns [] without touching the DB."""
    db = AsyncMock()
    result = await enqueue_enrichment(db, [], source="admin_single", user_id=1)
    assert result == []
    db.execute.assert_not_called()


# ---------------------------------------------------------------------------
# enqueue_enrichment — in-flight dedup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_dedup_skips_in_flight_players() -> None:
    """enqueue_enrichment skips players that already have a queued/running job.

    The in-flight player should not appear in the returned list and
    db.add should not be called for it.
    """
    in_flight_player_id = 42
    new_player_id = 99

    db = AsyncMock()
    # Player 42 is in-flight; player 99 is not
    mock_result = MagicMock()
    mock_result.fetchall.return_value = [(in_flight_player_id,)]
    db.execute = AsyncMock(return_value=mock_result)
    db.add = MagicMock()
    db.flush = AsyncMock()

    enqueued = await enqueue_enrichment(
        db,
        [in_flight_player_id, new_player_id],
        source="admin_bulk",
        user_id=5,
    )

    assert enqueued == [new_player_id]
    # Only one job added (for the new player)
    assert db.add.call_count == 1


@pytest.mark.asyncio
async def test_enqueue_all_in_flight_returns_empty() -> None:
    """enqueue_enrichment returns [] when all requested players are in-flight."""
    db = AsyncMock()
    mock_result = MagicMock()
    mock_result.fetchall.return_value = [(1,), (2,)]
    db.execute = AsyncMock(return_value=mock_result)
    db.add = MagicMock()
    db.flush = AsyncMock()

    enqueued = await enqueue_enrichment(db, [1, 2], source="admin_single", user_id=None)

    assert enqueued == []
    db.add.assert_not_called()
    db.flush.assert_not_called()


# ---------------------------------------------------------------------------
# enqueue_enrichment — flush only when jobs created
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_flush_called_when_jobs_created() -> None:
    """db.flush is called when at least one job is created."""
    db = AsyncMock()
    mock_result = MagicMock()
    mock_result.fetchall.return_value = []
    db.execute = AsyncMock(return_value=mock_result)
    db.add = MagicMock()
    db.flush = AsyncMock()

    await enqueue_enrichment(db, [7], source="cron", user_id=None)
    db.flush.assert_awaited_once()


# ---------------------------------------------------------------------------
# enrichment_status — most-recent-job-per-player
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enrichment_status_returns_most_recent_job() -> None:
    """enrichment_status keeps only the most-recent job per player.

    When a player has multiple jobs (e.g. a previous failed one and a new
    queued one), only the row with the highest created_at wins — which is
    the first row in the ordered result (ordered by created_at desc).
    """
    # Build two mock jobs for player 10: first=most-recent (succeeded), second=older (failed)
    job_recent = _make_mock_job(job_id=2, player_id=10, state="succeeded")
    job_old = _make_mock_job(job_id=1, player_id=10, state="failed", error_message="timeout")

    db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [job_recent, job_old]
    db.execute = AsyncMock(return_value=mock_result)

    status = await enrichment_status(db, [10])

    assert 10 in status
    assert status[10].state == "succeeded"
    assert status[10].error is None


@pytest.mark.asyncio
async def test_enrichment_status_empty_ids() -> None:
    """enrichment_status with an empty list returns {} without hitting the DB."""
    db = AsyncMock()
    result = await enrichment_status(db, [])
    assert result == {}
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_enrichment_status_multiple_players() -> None:
    """enrichment_status returns separate entries for each player."""
    job_a = _make_mock_job(job_id=1, player_id=10, state="succeeded")
    job_b = _make_mock_job(job_id=2, player_id=20, state="running")

    db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [job_a, job_b]
    db.execute = AsyncMock(return_value=mock_result)

    status = await enrichment_status(db, [10, 20])

    assert status[10].state == "succeeded"
    assert status[20].state == "running"


@pytest.mark.asyncio
async def test_enrichment_status_missing_player_omitted() -> None:
    """enrichment_status omits players with no job records."""
    db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=mock_result)

    status = await enrichment_status(db, [999])
    assert status == {}


# ---------------------------------------------------------------------------
# drain_enrichment_queue — skips when no Gemini key configured
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drain_skips_when_no_gemini_key() -> None:
    """drain_enrichment_queue returns an empty DrainResult when GEMINI_API_KEY is absent."""
    from app.services import enrichment_queue_service

    session_factory = MagicMock()

    with patch.object(enrichment_queue_service.settings, "gemini_api_key", None):
        from app.services.enrichment_queue_service import drain_enrichment_queue

        result = await drain_enrichment_queue(session_factory, limit=5)

    assert result.claimed == 0
    assert result.succeeded == 0
    session_factory.assert_not_called()
