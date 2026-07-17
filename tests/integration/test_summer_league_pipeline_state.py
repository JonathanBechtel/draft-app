"""Integration coverage for durable Summer League cron coordination state."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.summer_league_pipeline import (
    SummerLeaguePipelineJob,
    SummerLeaguePipelineOutcome,
    SummerLeaguePipelineState,
)
from app.services.summer_league.pipeline_state import (
    complete_pipeline,
    defer_full_reconciliation,
    full_reconciliation_is_pending,
    record_pipeline_failure,
)

pytestmark = pytest.mark.asyncio


async def test_full_lock_deferrals_are_durable_until_a_later_rebuild_completes(
    db_session: AsyncSession,
) -> None:
    """Two lock conflicts persist retry work, then a completed rebuild clears it."""
    first = datetime(2026, 7, 17, 12, 0)
    second = datetime(2026, 7, 17, 13, 0)
    completed = datetime(2026, 7, 17, 14, 0)

    await defer_full_reconciliation(
        db_session, reason="venue:15:shared_write_phase_lock_contended", now=first
    )
    await defer_full_reconciliation(
        db_session, reason="metrics_and_snapshot_lock_contended", now=second
    )
    await db_session.commit()

    assert await full_reconciliation_is_pending(db_session) is True
    state = (
        await db_session.execute(
            select(SummerLeaguePipelineState).where(
                SummerLeaguePipelineState.job == SummerLeaguePipelineJob.FULL_INGESTION
            )
        )
    ).scalar_one()
    assert state.last_outcome == SummerLeaguePipelineOutcome.DEFERRED
    assert state.consecutive_deferrals == 2
    assert state.last_deferred_at == second

    await complete_pipeline(
        db_session,
        job=SummerLeaguePipelineJob.FULL_INGESTION,
        metrics_rebuilt=True,
        snapshots_materialized=True,
        now=completed,
    )
    await db_session.commit()

    assert await full_reconciliation_is_pending(db_session) is False
    await db_session.commit()
    refreshed = (
        await db_session.execute(select(SummerLeaguePipelineState))
    ).scalar_one()
    assert refreshed.last_outcome == SummerLeaguePipelineOutcome.SUCCEEDED
    assert refreshed.consecutive_deferrals == 0
    assert refreshed.last_metrics_rebuilt_at == completed
    assert refreshed.last_snapshots_materialized_at == completed


async def test_pipeline_failure_is_queryable_without_erasing_pending_work(
    db_session: AsyncSession,
) -> None:
    """A failed retry remains observable and cannot silently discard deferred work."""
    await defer_full_reconciliation(
        db_session,
        reason="venue:15:shared_write_phase_lock_contended",
        now=datetime(2026, 7, 17, 12, 0),
    )
    await record_pipeline_failure(
        db_session,
        job=SummerLeaguePipelineJob.FULL_INGESTION,
        reason="metrics rebuild failed: database unavailable",
        now=datetime(2026, 7, 17, 13, 0),
    )
    await db_session.commit()

    assert await full_reconciliation_is_pending(db_session) is True
    await db_session.commit()
    state = (await db_session.execute(select(SummerLeaguePipelineState))).scalar_one()
    assert state.last_outcome == SummerLeaguePipelineOutcome.FAILED
    assert state.last_failure_reason == "metrics rebuild failed: database unavailable"
