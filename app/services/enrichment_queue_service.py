"""Enrichment job queue — enqueue, drain, and status helpers.

Backs the on-demand enrichment feature (§C of the Stub Player Management
spec).  The queue is DB-backed via ``PlayerEnrichmentJob`` so that jobs
survive web-machine restarts and can be drained by the cron backstop.

Concurrency model
-----------------
``drain_enrichment_queue`` claims ``queued`` rows with
``FOR UPDATE SKIP LOCKED`` so multiple callers (a ``BackgroundTasks``
invocation and the cron runner) cannot double-process the same job.
Stale ``running`` rows (older than ``_STALE_RUNNING_SECONDS``) are
reclaimed at the start of each drain so a machine restart never leaves
jobs permanently stuck.

Per-request cap
---------------
``enqueue_enrichment`` refuses to enqueue more than ``MAX_ENQUEUE_PER_REQUEST``
players in a single call.  Callers receive the truncated list; the flash
message should surface the cap to avoid silent truncation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from google import genai
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.schemas.player_enrichment_jobs import PlayerEnrichmentJob
from app.schemas.players_master import PlayerMaster
from app.services.player_enrichment_service import _enrich_player_with_factory

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------

#: Maximum number of players a single ``enqueue_enrichment`` call may queue.
MAX_ENQUEUE_PER_REQUEST: int = 25

#: Running jobs older than this are treated as stale and reclaimed.
_STALE_RUNNING_SECONDS: int = 600  # 10 minutes


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class JobStatus:
    """Status of the most-recent enrichment job for a player."""

    state: str  # queued | running | succeeded | failed
    error: Optional[str] = None


@dataclass
class DrainResult:
    """Summary of a ``drain_enrichment_queue`` call."""

    claimed: int = 0
    succeeded: int = 0
    failed: int = 0
    reclaimed_stale: int = 0
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Enqueue
# ---------------------------------------------------------------------------


async def enqueue_enrichment(
    db: AsyncSession,
    player_ids: list[int],
    *,
    source: str,
    user_id: Optional[int],
) -> list[int]:
    """Create ``PlayerEnrichmentJob`` rows for each eligible player.

    Players that already have a ``queued`` or ``running`` job are skipped
    (dedup).  The list is silently truncated to ``MAX_ENQUEUE_PER_REQUEST``
    before the dedup check — callers should surface this to users.

    Args:
        db: Active database session (caller owns the transaction / commit).
        player_ids: Candidate player primary keys to enqueue.
        source: Job origin label (``admin_single`` / ``admin_bulk`` / ``cron``).
        user_id: Admin user who triggered the request, or ``None`` for cron.

    Returns:
        List of player IDs for which a new job was created (subset of
        input after dedup and cap).
    """
    # Apply per-request cap
    capped = player_ids[:MAX_ENQUEUE_PER_REQUEST]
    if len(player_ids) > MAX_ENQUEUE_PER_REQUEST:
        logger.warning(
            "enqueue_enrichment: %d player IDs supplied, truncated to %d",
            len(player_ids),
            MAX_ENQUEUE_PER_REQUEST,
        )

    if not capped:
        return []

    # Find players that already have an in-flight job
    in_flight_stmt = (
        select(PlayerEnrichmentJob.player_id)  # type: ignore[call-overload]
        .where(PlayerEnrichmentJob.player_id.in_(capped))  # type: ignore[attr-defined]
        .where(PlayerEnrichmentJob.state.in_(["queued", "running"]))  # type: ignore[attr-defined]
    )
    result = await db.execute(in_flight_stmt)
    in_flight_ids = {row[0] for row in result.fetchall()}

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    enqueued: list[int] = []

    for pid in capped:
        if pid in in_flight_ids:
            logger.debug("Player id=%d already has an in-flight job, skipping", pid)
            continue
        job = PlayerEnrichmentJob(
            player_id=pid,
            state="queued",
            source=source,
            requested_by_user_id=user_id,
            created_at=now,
        )
        db.add(job)
        enqueued.append(pid)

    if enqueued:
        await db.flush()
        logger.info("Enqueued %d enrichment jobs (source=%s)", len(enqueued), source)

    return enqueued


# ---------------------------------------------------------------------------
# Drain
# ---------------------------------------------------------------------------


async def drain_enrichment_queue(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    limit: int = MAX_ENQUEUE_PER_REQUEST,
) -> DrainResult:
    """Claim and process up to ``limit`` queued enrichment jobs.

    Follows the ``SessionLocal()``-per-task pattern from ``email_worker``
    and the ``FOR UPDATE SKIP LOCKED`` pattern from ``ImageBatchJob``.

    Steps:
    1. Reclaim stale ``running`` jobs (machine-restart safety).
    2. Claim up to ``limit`` ``queued`` rows, transition to ``running``.
    3. For each claimed job, call the enrichment core.
    4. Set terminal state (``succeeded`` / ``failed``) with timestamps.

    Args:
        session_factory: Factory used for all DB access; this function
            owns its own sessions (safe to call from ``BackgroundTasks``
            or the cron runner).
        limit: Maximum number of jobs to process in this call.

    Returns:
        ``DrainResult`` with counts and any error messages.
    """
    drain_result = DrainResult()

    if not settings.gemini_api_key:
        logger.warning("GEMINI_API_KEY not configured, skipping drain")
        return drain_result

    client = genai.Client(api_key=settings.gemini_api_key)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    stale_cutoff = now - timedelta(seconds=_STALE_RUNNING_SECONDS)

    # Step 1: Reclaim stale running jobs
    async with session_factory() as db:
        async with db.begin():
            stale_result = await db.execute(
                update(PlayerEnrichmentJob)  # type: ignore[arg-type]
                .where(PlayerEnrichmentJob.state == "running")  # type: ignore[arg-type]
                .where(PlayerEnrichmentJob.started_at <= stale_cutoff)  # type: ignore[operator,arg-type]
                .values(state="queued", started_at=None)
                .returning(PlayerEnrichmentJob.id)  # type: ignore[call-overload]
            )
            reclaimed_ids = stale_result.fetchall()
            drain_result.reclaimed_stale = len(reclaimed_ids)
            if reclaimed_ids:
                logger.info(
                    "Reclaimed %d stale running enrichment jobs", len(reclaimed_ids)
                )

    # Step 2: Claim queued jobs with FOR UPDATE SKIP LOCKED
    claim_stmt = (
        select(PlayerEnrichmentJob)  # type: ignore[call-overload]
        .where(PlayerEnrichmentJob.state == "queued")  # type: ignore[arg-type]
        .order_by(PlayerEnrichmentJob.created_at)  # type: ignore[arg-type]
        .limit(limit)
        .with_for_update(skip_locked=True)
    )

    async with session_factory() as db:
        async with db.begin():
            claim_result = await db.execute(claim_stmt)
            jobs = list(claim_result.scalars().all())

            if not jobs:
                return drain_result

            drain_result.claimed = len(jobs)
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            for job in jobs:
                job.state = "running"
                job.started_at = now

    # Step 3 + 4: Process each job in its own session
    for job in jobs:
        job_id = job.id
        player_id = job.player_id

        # Look up the player's display_name for the enrichment core
        async with session_factory() as db:
            player = await db.get(PlayerMaster, player_id)
            if player is None:
                logger.error(
                    "Enrichment job %d references missing player %d", job_id, player_id
                )
                async with session_factory() as err_db:
                    async with err_db.begin():
                        err_job = await err_db.get(PlayerEnrichmentJob, job_id)
                        if err_job is not None:
                            _set_terminal_state(
                                err_job,
                                "failed",
                                "Player not found",
                                datetime.now(timezone.utc).replace(tzinfo=None),
                            )
                drain_result.failed += 1
                drain_result.errors.append(
                    f"job {job_id}: player {player_id} not found"
                )
                continue
            name = player.display_name or ""

        if not name:
            logger.error(
                "Enrichment job %d: player %d has no display_name", job_id, player_id
            )
            async with session_factory() as err_db:
                async with err_db.begin():
                    err_job = await err_db.get(PlayerEnrichmentJob, job_id)
                    if err_job is not None:
                        _set_terminal_state(
                            err_job,
                            "failed",
                            "Player has no display_name",
                            datetime.now(timezone.utc).replace(tzinfo=None),
                        )
            drain_result.failed += 1
            drain_result.errors.append(
                f"job {job_id}: player {player_id} has no display_name"
            )
            continue

        logger.info(
            "Draining enrichment job %d for player %d (%s)", job_id, player_id, name
        )

        try:
            single = await _enrich_player_with_factory(
                session_factory, player_id, name, client
            )
        except Exception as exc:
            single_error = str(exc)
            logger.exception("Enrichment job %d failed with exception", job_id)
            async with session_factory() as err_db:
                async with err_db.begin():
                    err_job = await err_db.get(PlayerEnrichmentJob, job_id)
                    if err_job is not None:
                        _set_terminal_state(
                            err_job,
                            "failed",
                            single_error,
                            datetime.now(timezone.utc).replace(tzinfo=None),
                        )
            drain_result.failed += 1
            drain_result.errors.append(f"job {job_id}: {single_error}")
            continue

        completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        if single.error:
            terminal_state = "failed"
            drain_result.failed += 1
            drain_result.errors.append(f"job {job_id}: {single.error}")
        else:
            terminal_state = "succeeded"
            drain_result.succeeded += 1

        async with session_factory() as done_db:
            async with done_db.begin():
                done_job = await done_db.get(PlayerEnrichmentJob, job_id)
                if done_job is not None:
                    _set_terminal_state(
                        done_job, terminal_state, single.error, completed_at
                    )

    return drain_result


def _set_terminal_state(
    job: PlayerEnrichmentJob,
    state: str,
    error: Optional[str],
    completed_at: datetime,
) -> None:
    """Set a job's terminal state fields in-place (no flush/commit)."""
    job.state = state
    job.completed_at = completed_at
    job.error_message = error


# ---------------------------------------------------------------------------
# Status polling
# ---------------------------------------------------------------------------


async def enrichment_status(
    db: AsyncSession,
    player_ids: list[int],
) -> dict[int, JobStatus]:
    """Return the most-recent job status for each requested player.

    Players with no job record are omitted from the result.

    Args:
        db: Active database session (read-only).
        player_ids: Player primary keys to poll.

    Returns:
        Mapping ``{player_id: JobStatus}`` for players that have at least
        one enrichment job.  The most recently created job wins.
    """
    if not player_ids:
        return {}

    # Fetch all jobs for the requested players, ordered by created_at desc
    stmt = (
        select(PlayerEnrichmentJob)  # type: ignore[call-overload]
        .where(PlayerEnrichmentJob.player_id.in_(player_ids))  # type: ignore[attr-defined]
        .order_by(PlayerEnrichmentJob.player_id, PlayerEnrichmentJob.created_at.desc())  # type: ignore[arg-type,attr-defined]
    )
    result = await db.execute(stmt)
    jobs = result.scalars().all()

    # Keep only the most-recent job per player
    status: dict[int, JobStatus] = {}
    for job in jobs:
        if job.player_id not in status:
            status[job.player_id] = JobStatus(state=job.state, error=job.error_message)

    return status
