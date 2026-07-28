"""Content-aware gate for the expensive Summer League metrics rebuild."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.event_desk.render_snapshots import upsert_render_snapshots
from app.services.event_desk.snapshot_materialization import (
    prepare_desk_render_snapshots,
)
from app.services.summer_league.environment_refresh import (
    refresh_environment_profiles_for_year,
    resolve_environment_refresh_scope,
)
from app.services.summer_league.metrics import rebuild_staged as rebuild_sl_metrics
from app.services.summer_league.metric_publish import publish_metric_version
from app.services.summer_league.metrics_input import calculate_metrics_input_watermark
from app.services.summer_league.pipeline_state import (
    SummerLeaguePipelineJob,
    complete_pipeline,
    defer_full_reconciliation,
    full_reconciliation_is_pending,
    get_pipeline_freshness,
)
from app.services.summer_league.pipeline_telemetry import PipelineTelemetry
from app.services.summer_league.write_lock import (
    try_acquire_summer_league_writer_lock_yielding,
)

logger = logging.getLogger("summer_league_ingest_runner")


@dataclass(frozen=True)
class MetricsStageContext:
    """Stable run inputs needed to decide and execute the derivative stage."""

    year: int
    any_games: bool
    upstream_succeeded: bool
    telemetry: PipelineTelemetry
    force_reconcile: bool = False


async def run_metrics_stage(
    db: AsyncSession,
    *,
    context: MetricsStageContext,
    before_derivatives: Callable[[], Awaitable[None]],
) -> bool:
    """Rebuild metrics only when their durable input watermark changed.

    Returns:
        Whether the derivative stage failed.
    """
    pending_reconciliation = await full_reconciliation_is_pending(db)
    current_input_watermark = await calculate_metrics_input_watermark(db)
    pipeline_state = await get_pipeline_freshness(
        db,
        SummerLeaguePipelineJob.FULL_INGESTION,
    )
    previous_input_watermark = (
        pipeline_state.last_metrics_input_watermark
        if pipeline_state is not None
        else None
    )
    inputs_changed = current_input_watermark != previous_input_watermark
    # The state read auto-begins a transaction. End that read-only transaction
    # before the explicit serialized derivative phase.
    await before_derivatives()

    if not context.upstream_succeeded:
        logger.warning(
            "SL metrics rebuild skipped: upstream normalization failed; "
            "input watermark remains unchanged"
        )
        return False

    if (
        not pending_reconciliation
        and not context.force_reconcile
        and not inputs_changed
    ):
        logger.info(
            "SL metrics rebuild skipped: input watermark unchanged watermark=%s",
            current_input_watermark,
        )
        if context.any_games:
            async with db.begin():
                await complete_pipeline(
                    db,
                    job=SummerLeaguePipelineJob.FULL_INGESTION,
                    metrics_rebuilt=False,
                    snapshots_materialized=False,
                )
        else:
            logger.info("No venue had games and no metrics inputs changed")
        return False

    logger.info(
        "SL metrics rebuild required: reason=%s input_watermark=%s "
        "previous_watermark=%s",
        (
            "operator_full_reconcile"
            if context.force_reconcile
            else (
                "pending_reconciliation"
                if pending_reconciliation
                else "input_watermark_changed"
            )
        ),
        current_input_watermark,
        previous_input_watermark,
    )
    try:
        with context.telemetry.step("metrics_and_snapshots") as step_fields:
            # Build the candidate projection in its own transaction. This is the
            # expensive part of a rebuild, so it deliberately runs without the
            # shared writer lock; current rows remain readable throughout.
            async with db.begin():
                summary = await rebuild_sl_metrics(db)

            metrics_version = int(summary["version"])
            # Render variants must be assembled from the same candidate metrics
            # version that will be published. They are kept in memory until the
            # final short transaction so a failed build cannot affect the live
            # homepage cache.
            async with db.begin():
                snapshot_writes = await prepare_desk_render_snapshots(
                    db, metrics_version=metrics_version
                )

            target_year = resolve_environment_refresh_scope(
                year=context.year,
                any_games=context.any_games,
                pending_reconciliation=pending_reconciliation,
            )
            published = False
            # Only the pointer flip, cache writes, and pipeline bookkeeping are
            # serialized. If the Desk owns the lock, the candidate stays
            # inactive and the next scheduled run retries it.
            async with db.begin():
                if not await try_acquire_summer_league_writer_lock_yielding(
                    db, step_fields
                ):
                    await defer_full_reconciliation(
                        db,
                        reason="metrics_and_snapshot_lock_contended",
                    )
                    logger.info(
                        "SL metrics publication deferred (Desk writer is active); "
                        "the candidate remains invisible for a later scheduled run"
                    )
                else:
                    await publish_metric_version(
                        db,
                        version=metrics_version,
                        model_version=str(summary["model_version"]),
                    )
                    await upsert_render_snapshots(db, snapshot_writes)
                    await complete_pipeline(
                        db,
                        job=SummerLeaguePipelineJob.FULL_INGESTION,
                        metrics_rebuilt=True,
                        snapshots_materialized=bool(snapshot_writes),
                        metrics_input_watermark=current_input_watermark,
                    )
                    published = True

            if published:
                logger.info(
                    "SL metrics rebuild complete: %s player-seasons, "
                    "%s contexts (%s adv-eligible pools); refreshed "
                    "%s Desk render snapshots",
                    summary["seasons"],
                    summary["contexts"],
                    summary["adv_pools"],
                    len(snapshot_writes),
                )
                # Competition Context is an independent, versioned projection.
                # Its own service acquires the writer lock before reading facts,
                # so keep it outside the metrics pointer-flip transaction.
                if target_year is not None:
                    async with db.begin():
                        await refresh_environment_profiles_for_year(
                            db, year=target_year, telemetry=context.telemetry
                        )
    except Exception as exc:
        logger.error("SL metrics rebuild failed: %s", exc, exc_info=True)
        return True
    return False
