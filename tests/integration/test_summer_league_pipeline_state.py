"""Integration coverage for durable Summer League cron coordination state."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.summer_league_pipeline import (
    SummerLeagueBatchPhase,
    SummerLeaguePipelineJob,
    SummerLeaguePipelineOutcome,
    SummerLeaguePipelineState,
)
from app.services.summer_league.batch_progress import (
    get_completed_batch_game_ids,
    record_batch_progress,
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


# ---------------------------------------------------------------------------
# Batch-progress tracking (app.services.summer_league.batch_progress)
# ---------------------------------------------------------------------------


async def test_get_completed_batch_game_ids_empty_before_any_batch(
    db_session: AsyncSession,
) -> None:
    """No progress rows yet -- the completed set is empty for a fresh phase/venue/year."""
    completed = await get_completed_batch_game_ids(
        db_session, year=2026, league_id="15", phase=SummerLeagueBatchPhase.SHOT
    )
    assert completed == set()


async def test_record_batch_progress_is_durable_and_scoped_by_phase(
    db_session: AsyncSession,
) -> None:
    """A committed batch's game IDs are readable back, scoped to their own phase.

    Proves the resumability contract: a game recorded complete for the SHOT
    phase does not leak into the PBP phase's completed set for the same
    venue/year, and vice versa.
    """
    await record_batch_progress(
        db_session,
        year=2026,
        league_id="15",
        phase=SummerLeagueBatchPhase.SHOT,
        game_ids=["1522600001", "1522600002"],
    )
    await db_session.commit()

    shot_completed = await get_completed_batch_game_ids(
        db_session, year=2026, league_id="15", phase=SummerLeagueBatchPhase.SHOT
    )
    pbp_completed = await get_completed_batch_game_ids(
        db_session, year=2026, league_id="15", phase=SummerLeagueBatchPhase.PBP
    )

    assert shot_completed == {"1522600001", "1522600002"}
    assert pbp_completed == set()


async def test_record_batch_progress_scoped_by_year_and_league(
    db_session: AsyncSession,
) -> None:
    """Progress is scoped per (year, league_id, phase); other slices stay unaffected."""
    await record_batch_progress(
        db_session,
        year=2026,
        league_id="15",
        phase=SummerLeagueBatchPhase.PBP,
        game_ids=["1522600001"],
    )
    await db_session.commit()

    other_league = await get_completed_batch_game_ids(
        db_session, year=2026, league_id="13", phase=SummerLeagueBatchPhase.PBP
    )
    other_year = await get_completed_batch_game_ids(
        db_session, year=2025, league_id="15", phase=SummerLeagueBatchPhase.PBP
    )

    assert other_league == set()
    assert other_year == set()


async def test_record_batch_progress_is_idempotent_on_replay(
    db_session: AsyncSession,
) -> None:
    """Recording the same game ID twice for one phase does not error or duplicate.

    A crashed batch that partially wrote progress before failing, followed
    by a clean re-run of the same batch, must not raise a unique-constraint
    violation -- ``on_conflict_do_nothing`` makes this safe.
    """
    await record_batch_progress(
        db_session,
        year=2026,
        league_id="15",
        phase=SummerLeagueBatchPhase.SHOT,
        game_ids=["1522600001"],
    )
    await db_session.commit()

    await record_batch_progress(
        db_session,
        year=2026,
        league_id="15",
        phase=SummerLeagueBatchPhase.SHOT,
        game_ids=["1522600001", "1522600002"],
    )
    await db_session.commit()

    completed = await get_completed_batch_game_ids(
        db_session, year=2026, league_id="15", phase=SummerLeagueBatchPhase.SHOT
    )
    assert completed == {"1522600001", "1522600002"}


async def test_record_batch_progress_empty_game_ids_is_a_no_op(
    db_session: AsyncSession,
) -> None:
    """Recording an empty batch writes nothing and does not error."""
    await record_batch_progress(
        db_session,
        year=2026,
        league_id="15",
        phase=SummerLeagueBatchPhase.SHOT,
        game_ids=[],
    )
    await db_session.commit()

    completed = await get_completed_batch_game_ids(
        db_session, year=2026, league_id="15", phase=SummerLeagueBatchPhase.SHOT
    )
    assert completed == set()
