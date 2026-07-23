"""Durable coordination state for Summer League scheduled writers.

The advisory lock protects shared player/game/projection writes.  This service
records what happens when the lower-priority full ingestor cannot take that
lock, so the next scheduled run has explicit work to recover instead of relying
on a log line and luck.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.summer_league_pipeline import (
    SummerLeaguePipelineJob,
    SummerLeaguePipelineOutcome,
    SummerLeaguePipelineState,
)

_STATE_TABLE = getattr(SummerLeaguePipelineState, "__table__")


async def _state_for(
    db: AsyncSession, job: SummerLeaguePipelineJob
) -> SummerLeaguePipelineState:
    """Return the durable state for ``job``, creating it on first use."""
    result = await db.execute(
        select(SummerLeaguePipelineState).where(_STATE_TABLE.c.job == job)
    )
    state = result.scalar_one_or_none()
    if state is not None:
        return state

    state = SummerLeaguePipelineState(job=job)
    db.add(state)
    await db.flush()
    return state


async def defer_full_reconciliation(
    db: AsyncSession,
    *,
    reason: str,
    now: datetime | None = None,
) -> SummerLeaguePipelineState:
    """Record one bounded, next-scheduled-run retry request for the full job."""
    observed_at = now or datetime.utcnow()
    state = await _state_for(db, SummerLeaguePipelineJob.FULL_INGESTION)
    state.pending_reconciliation = True
    state.consecutive_deferrals += 1
    state.last_deferred_at = observed_at
    state.last_outcome = SummerLeaguePipelineOutcome.DEFERRED
    state.last_failure_reason = reason
    state.updated_at = observed_at
    await db.flush()
    return state


async def full_reconciliation_is_pending(db: AsyncSession) -> bool:
    """Return whether a previously deferred full rebuild must be drained."""
    result = await db.execute(
        select(_STATE_TABLE.c.pending_reconciliation).where(
            _STATE_TABLE.c.job == SummerLeaguePipelineJob.FULL_INGESTION
        )
    )
    return bool(result.scalar_one_or_none())


async def get_pipeline_freshness(
    db: AsyncSession, job: SummerLeaguePipelineJob
) -> SummerLeaguePipelineState | None:
    """Return one job's durable freshness/outcome state, or ``None`` if it never ran.

    Both scheduled writers keep their own row here, keyed by ``job``
    (:data:`SummerLeaguePipelineJob.DESK` / ``.FULL_INGESTION``), each with
    its own ``last_succeeded_at``/``last_metrics_rebuilt_at``/
    ``last_snapshots_materialized_at`` -- so the Desk tick's freshness is
    already distinguishable from full ingestion's by construction (see
    :func:`complete_pipeline`/:func:`_state_for`). This is the read-side
    counterpart: a single queryable entry point for either job's state,
    rather than every caller hand-rolling the same ``select(...).where(job
    == ...)`` this module already uses internally.

    Args:
        db: Active database session.
        job: Which scheduled writer's state to read.

    Returns:
        The durable state row for ``job``, or ``None`` if that job has never
        completed, deferred, or failed a run.
    """
    result = await db.execute(
        select(SummerLeaguePipelineState).where(_STATE_TABLE.c.job == job)
    )
    return result.scalar_one_or_none()


async def start_pipeline(
    db: AsyncSession,
    *,
    job: SummerLeaguePipelineJob,
    job_image: str | None = None,
    now: datetime | None = None,
) -> SummerLeaguePipelineState:
    """Record scheduler start time and the image executing this invocation."""
    started_at = now or datetime.utcnow()
    state = await _state_for(db, job)
    state.last_started_at = started_at
    state.last_job_image = job_image
    state.updated_at = started_at
    await db.flush()
    return state


async def complete_pipeline(
    db: AsyncSession,
    *,
    job: SummerLeaguePipelineJob,
    metrics_rebuilt: bool,
    snapshots_materialized: bool,
    source_refreshed: bool = False,
    source_advanced: bool = False,
    projections_refreshed: bool = False,
    content_updated: bool = False,
    now: datetime | None = None,
) -> SummerLeaguePipelineState:
    """Record scheduler success and independently advance useful-work signals."""
    completed_at = now or datetime.utcnow()
    state = await _state_for(db, job)
    state.last_outcome = SummerLeaguePipelineOutcome.SUCCEEDED
    state.last_completed_at = completed_at
    state.last_succeeded_at = completed_at
    state.last_content_updated = content_updated
    state.last_failure_reason = None
    state.updated_at = completed_at
    if source_refreshed:
        state.last_source_refreshed_at = completed_at
    if source_advanced:
        state.last_source_advanced_at = completed_at
    if projections_refreshed:
        state.last_projection_refreshed_at = completed_at
    if metrics_rebuilt:
        state.last_metrics_rebuilt_at = completed_at
    if snapshots_materialized:
        state.last_snapshots_materialized_at = completed_at
    if job == SummerLeaguePipelineJob.FULL_INGESTION:
        state.pending_reconciliation = False
        state.consecutive_deferrals = 0
    await db.flush()
    return state


async def record_pipeline_failure(
    db: AsyncSession,
    *,
    job: SummerLeaguePipelineJob,
    reason: str,
    now: datetime | None = None,
) -> SummerLeaguePipelineState:
    """Persist a scheduled-job failure without changing pending work."""
    failed_at = now or datetime.utcnow()
    state = await _state_for(db, job)
    state.last_outcome = SummerLeaguePipelineOutcome.FAILED
    state.last_completed_at = failed_at
    state.last_content_updated = False
    state.last_failure_at = failed_at
    state.last_failure_reason = reason
    state.updated_at = failed_at
    await db.flush()
    return state
