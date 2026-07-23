"""Shared writer for the persisted Event Desk render-snapshot matrix.

The homepage Class Tracker reads the render-snapshot matrix rather than live
``summer_league_player_seasons`` rows. Every full metrics rebuild must therefore
replace the matrix before its transaction commits, or direct stat surfaces and
the homepage can disagree until the next Desk tick.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.event_desk.render_snapshots import (
    RenderSnapshotWrite,
    upsert_render_snapshots,
)
from app.services.summer_league.desk_read import build_desk_render_variants


async def materialize_desk_render_snapshots(db: AsyncSession) -> int:
    """Rebuild the homepage Desk variants from the current materialized data.

    Returns zero outside the active Desk window; otherwise atomically upserts
    every daily-state, cohort, and stat-view variant. The caller owns the
    surrounding transaction.
    """
    result = await build_desk_render_variants(db, scheduled_write=True)
    if result is None:
        return 0

    event_id, variants = result
    writes = [
        RenderSnapshotWrite(
            event_id=event_id,
            daily_state=variant.daily_state,
            tracker_cohort=variant.tracker_cohort,
            tracker_stat_view=variant.tracker_stat_view,
            view=variant.view,
            source_freshness_tick_at=(
                variant.view.payload.freshness.last_tick_at
                if variant.view.payload is not None
                else None
            ),
            source_freshness_next_tick_eta=(
                variant.view.payload.freshness.next_tick_eta
                if variant.view.payload is not None
                else None
            ),
        )
        for variant in variants
    ]
    now_naive_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    await upsert_render_snapshots(db, writes, now=now_naive_utc)
    return len(writes)


__all__ = ["materialize_desk_render_snapshots"]
