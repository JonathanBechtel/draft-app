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
    count_pending_batch_games,
    get_completed_batch_game_ids,
    invalidate_batch_progress,
    record_batch_progress,
)
from app.services.summer_league.pipeline_state import (
    complete_pipeline,
    defer_full_reconciliation,
    full_reconciliation_is_pending,
    get_pipeline_freshness,
    record_pipeline_failure,
    start_pipeline,
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
        metrics_input_watermark="watermark-v1",
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
    assert refreshed.last_metrics_input_watermark == "watermark-v1"
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
# get_pipeline_freshness -- Desk-specific freshness distinguishable from
# FULL_INGESTION's (#629)
# ---------------------------------------------------------------------------


async def test_get_pipeline_freshness_none_before_a_job_has_ever_run(
    db_session: AsyncSession,
) -> None:
    """A job that has never completed/deferred/failed has no durable state yet."""
    assert (
        await get_pipeline_freshness(db_session, SummerLeaguePipelineJob.DESK) is None
    )


async def test_desk_and_full_ingestion_freshness_are_independently_tracked(
    db_session: AsyncSession,
) -> None:
    """Desk and FULL_INGESTION freshness never leak into each other's row.

    The Desk tick's own last-succeeded/materialized timestamps never leak
    into FULL_INGESTION's row, and vice versa -- each scheduled writer's
    freshness is distinguishable by `job`, not folded into one shared row.
    """
    desk_completed = datetime(2026, 7, 19, 18, 0)
    full_completed = datetime(2026, 7, 19, 12, 0)

    await complete_pipeline(
        db_session,
        job=SummerLeaguePipelineJob.DESK,
        metrics_rebuilt=False,
        snapshots_materialized=True,
        now=desk_completed,
    )
    await complete_pipeline(
        db_session,
        job=SummerLeaguePipelineJob.FULL_INGESTION,
        metrics_rebuilt=True,
        snapshots_materialized=True,
        now=full_completed,
    )
    await db_session.commit()

    desk_state = await get_pipeline_freshness(db_session, SummerLeaguePipelineJob.DESK)
    full_state = await get_pipeline_freshness(
        db_session, SummerLeaguePipelineJob.FULL_INGESTION
    )

    assert desk_state is not None and full_state is not None
    assert desk_state.last_succeeded_at == desk_completed
    assert desk_state.last_snapshots_materialized_at == desk_completed
    assert (
        desk_state.last_metrics_rebuilt_at is None
    )  # Desk tick never rebuilds metrics

    assert full_state.last_succeeded_at == full_completed
    assert full_state.last_snapshots_materialized_at == full_completed
    assert full_state.last_metrics_rebuilt_at == full_completed


async def test_desk_scheduler_and_useful_refresh_signals_are_independent(
    db_session: AsyncSession,
) -> None:
    """A healthy dormant run completes without advancing content watermarks."""
    active_completed = datetime(2026, 7, 19, 12, 0)
    dormant_started = datetime(2026, 7, 22, 12, 0)
    dormant_completed = datetime(2026, 7, 22, 12, 1)
    await complete_pipeline(
        db_session,
        job=SummerLeaguePipelineJob.DESK,
        metrics_rebuilt=False,
        snapshots_materialized=True,
        source_refreshed=True,
        source_advanced=True,
        projections_refreshed=True,
        content_updated=True,
        now=active_completed,
    )
    await start_pipeline(
        db_session,
        job=SummerLeaguePipelineJob.DESK,
        job_image="registry/app:dormant",
        now=dormant_started,
    )
    state = await complete_pipeline(
        db_session,
        job=SummerLeaguePipelineJob.DESK,
        metrics_rebuilt=False,
        snapshots_materialized=False,
        content_updated=False,
        started_at=dormant_started,
        now=dormant_completed,
    )

    assert state.last_started_at == dormant_started
    assert state.last_completed_at == dormant_completed
    assert state.last_job_image == "registry/app:dormant"
    assert state.last_succeeded_at == dormant_completed
    assert state.last_content_updated is False
    assert state.last_source_refreshed_at == active_completed
    assert state.last_source_advanced_at == active_completed
    assert state.last_projection_refreshed_at == active_completed
    assert state.last_snapshots_materialized_at == active_completed


async def test_older_completion_cannot_overwrite_a_newer_pipeline_start(
    db_session: AsyncSession,
) -> None:
    """An overlapping older run cannot make the newer in-progress run look complete."""
    prior_completed = datetime(2026, 7, 19, 10, 0)
    older_started = datetime(2026, 7, 19, 11, 0)
    newer_started = datetime(2026, 7, 19, 11, 1)
    await complete_pipeline(
        db_session,
        job=SummerLeaguePipelineJob.DESK,
        metrics_rebuilt=False,
        snapshots_materialized=False,
        now=prior_completed,
    )
    await start_pipeline(
        db_session, job=SummerLeaguePipelineJob.DESK, now=older_started
    )
    await start_pipeline(
        db_session, job=SummerLeaguePipelineJob.DESK, now=newer_started
    )

    state = await complete_pipeline(
        db_session,
        job=SummerLeaguePipelineJob.DESK,
        metrics_rebuilt=False,
        snapshots_materialized=True,
        content_updated=True,
        started_at=older_started,
        now=datetime(2026, 7, 19, 11, 2),
    )

    assert state.last_started_at == newer_started
    assert state.last_completed_at == prior_completed
    assert state.last_outcome is None
    assert state.last_content_updated is None

    state = await complete_pipeline(
        db_session,
        job=SummerLeaguePipelineJob.DESK,
        metrics_rebuilt=False,
        snapshots_materialized=True,
        content_updated=True,
        started_at=newer_started,
        now=datetime(2026, 7, 19, 11, 3),
    )
    assert state.last_completed_at == datetime(2026, 7, 19, 11, 3)
    assert state.last_outcome == SummerLeaguePipelineOutcome.SUCCEEDED


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


# ---------------------------------------------------------------------------
# invalidate_batch_progress
# ---------------------------------------------------------------------------


async def test_invalidate_batch_progress_deletes_only_the_named_games(
    db_session: AsyncSession,
) -> None:
    """Invalidating a subset of games clears exactly those rows, nothing else.

    Proves the core correctness gap this ticket closes: a game whose row is
    deleted here re-enters `get_completed_batch_game_ids`'s complement (the
    "remaining" set `_run_batched_phase` computes) on the very next read.
    """
    await record_batch_progress(
        db_session,
        year=2026,
        league_id="15",
        phase=SummerLeagueBatchPhase.SHOT,
        game_ids=["1522600001", "1522600002", "1522600003"],
    )
    await db_session.commit()

    await invalidate_batch_progress(
        db_session,
        year=2026,
        league_id="15",
        phase=SummerLeagueBatchPhase.SHOT,
        game_ids=["1522600002"],
    )
    await db_session.commit()

    remaining = await get_completed_batch_game_ids(
        db_session, year=2026, league_id="15", phase=SummerLeagueBatchPhase.SHOT
    )
    assert remaining == {"1522600001", "1522600003"}


async def test_invalidate_batch_progress_scoped_to_phase_year_and_league(
    db_session: AsyncSession,
) -> None:
    """Invalidation never touches a different phase, year, or league slice."""
    await record_batch_progress(
        db_session,
        year=2026,
        league_id="15",
        phase=SummerLeagueBatchPhase.SHOT,
        game_ids=["1522600001"],
    )
    await record_batch_progress(
        db_session,
        year=2026,
        league_id="15",
        phase=SummerLeagueBatchPhase.PBP,
        game_ids=["1522600001"],
    )
    await record_batch_progress(
        db_session,
        year=2026,
        league_id="13",
        phase=SummerLeagueBatchPhase.SHOT,
        game_ids=["1522600001"],
    )
    await record_batch_progress(
        db_session,
        year=2025,
        league_id="15",
        phase=SummerLeagueBatchPhase.SHOT,
        game_ids=["1522600001"],
    )
    await db_session.commit()

    await invalidate_batch_progress(
        db_session,
        year=2026,
        league_id="15",
        phase=SummerLeagueBatchPhase.SHOT,
        game_ids=["1522600001"],
    )
    await db_session.commit()

    assert (
        await get_completed_batch_game_ids(
            db_session, year=2026, league_id="15", phase=SummerLeagueBatchPhase.SHOT
        )
        == set()
    )
    assert await get_completed_batch_game_ids(
        db_session, year=2026, league_id="15", phase=SummerLeagueBatchPhase.PBP
    ) == {"1522600001"}
    assert await get_completed_batch_game_ids(
        db_session, year=2026, league_id="13", phase=SummerLeagueBatchPhase.SHOT
    ) == {"1522600001"}
    assert await get_completed_batch_game_ids(
        db_session, year=2025, league_id="15", phase=SummerLeagueBatchPhase.SHOT
    ) == {"1522600001"}


async def test_invalidate_batch_progress_none_deletes_every_row_for_the_slice(
    db_session: AsyncSession,
) -> None:
    """game_ids=None (full-reconciliation mode) clears every row for one phase/venue/year."""
    await record_batch_progress(
        db_session,
        year=2026,
        league_id="15",
        phase=SummerLeagueBatchPhase.SHOT,
        game_ids=["1522600001", "1522600002"],
    )
    await record_batch_progress(
        db_session,
        year=2026,
        league_id="15",
        phase=SummerLeagueBatchPhase.PBP,
        game_ids=["1522600001"],
    )
    await db_session.commit()

    await invalidate_batch_progress(
        db_session, year=2026, league_id="15", phase=SummerLeagueBatchPhase.SHOT
    )
    await db_session.commit()

    assert (
        await get_completed_batch_game_ids(
            db_session, year=2026, league_id="15", phase=SummerLeagueBatchPhase.SHOT
        )
        == set()
    )
    # PBP is untouched -- full reconciliation invalidates one phase at a time.
    assert await get_completed_batch_game_ids(
        db_session, year=2026, league_id="15", phase=SummerLeagueBatchPhase.PBP
    ) == {"1522600001"}


async def test_invalidate_batch_progress_empty_game_ids_is_a_no_op(
    db_session: AsyncSession,
) -> None:
    """An empty explicit game_ids iterable deletes nothing, mirroring record_batch_progress."""
    await record_batch_progress(
        db_session,
        year=2026,
        league_id="15",
        phase=SummerLeagueBatchPhase.SHOT,
        game_ids=["1522600001"],
    )
    await db_session.commit()

    await invalidate_batch_progress(
        db_session,
        year=2026,
        league_id="15",
        phase=SummerLeagueBatchPhase.SHOT,
        game_ids=[],
    )
    await db_session.commit()

    assert await get_completed_batch_game_ids(
        db_session, year=2026, league_id="15", phase=SummerLeagueBatchPhase.SHOT
    ) == {"1522600001"}


# ---------------------------------------------------------------------------
# count_pending_batch_games
# ---------------------------------------------------------------------------


async def test_count_pending_batch_games_reflects_a_partial_run(
    db_session: AsyncSession,
) -> None:
    """Backlog count reflects discovered-but-not-yet-completed games."""
    await record_batch_progress(
        db_session,
        year=2026,
        league_id="15",
        phase=SummerLeagueBatchPhase.SHOT,
        game_ids=["1522600001"],
    )
    await db_session.commit()

    pending = await count_pending_batch_games(
        db_session,
        year=2026,
        league_id="15",
        phase=SummerLeagueBatchPhase.SHOT,
        discovered_game_ids=["1522600001", "1522600002", "1522600003"],
    )

    assert pending == 2


async def test_count_pending_batch_games_zero_when_everything_complete(
    db_session: AsyncSession,
) -> None:
    """A fully-completed slice has zero backlog."""
    await record_batch_progress(
        db_session,
        year=2026,
        league_id="15",
        phase=SummerLeagueBatchPhase.SHOT,
        game_ids=["1522600001", "1522600002"],
    )
    await db_session.commit()

    pending = await count_pending_batch_games(
        db_session,
        year=2026,
        league_id="15",
        phase=SummerLeagueBatchPhase.SHOT,
        discovered_game_ids=["1522600001", "1522600002"],
    )

    assert pending == 0


async def test_count_pending_batch_games_empty_discovered_set_is_zero(
    db_session: AsyncSession,
) -> None:
    """No discovered games at all -- zero backlog without a DB round trip's worth of noise."""
    pending = await count_pending_batch_games(
        db_session,
        year=2026,
        league_id="15",
        phase=SummerLeagueBatchPhase.SHOT,
        discovered_game_ids=[],
    )

    assert pending == 0
