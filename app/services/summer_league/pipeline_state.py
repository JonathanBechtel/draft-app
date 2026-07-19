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


async def complete_pipeline(
    db: AsyncSession,
    *,
    job: SummerLeaguePipelineJob,
    metrics_rebuilt: bool,
    snapshots_materialized: bool,
    now: datetime | None = None,
) -> SummerLeaguePipelineState:
    """Mark a completed job and clear a full-ingestion recovery request."""
    completed_at = now or datetime.utcnow()
    state = await _state_for(db, job)
    state.last_outcome = SummerLeaguePipelineOutcome.SUCCEEDED
    state.last_succeeded_at = completed_at
    state.last_failure_reason = None
    state.updated_at = completed_at
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
    state.last_failure_at = failed_at
    state.last_failure_reason = reason
    state.updated_at = failed_at
    await db.flush()
    return state
