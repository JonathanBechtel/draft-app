"""Recompute and persist the materialized Summer League advanced metrics.

Wipes and repopulates ``summer_league_metric_models`` / ``_metric_contexts`` /
``_player_seasons`` from the raw box logs. Safe to re-run; it is a full rebuild.

Run:
  scripts/with-db-env.sh conda run -n draftguru python scripts/rebuild_sl_metrics.py
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.event_desk.render_snapshots import (
    RenderSnapshotWrite,
    upsert_render_snapshots,
)
from app.services.summer_league.desk_read import build_desk_render_variants
from app.services.summer_league.metrics import rebuild
from app.utils.db_async import SessionLocal, engine


async def _materialize_desk_render_snapshots(db: AsyncSession) -> int:
    """Refresh homepage Desk snapshots after a full metrics rebuild.

    The Summer League Explorer and player pages read
    ``summer_league_player_seasons`` directly. The homepage Class Tracker is
    deliberately different: it reads one of the persisted Desk render
    snapshots. Rebuilding metrics without replacing those snapshots leaves
    the Class Tracker's Advanced tab on the previous eligibility/value state
    until a later hourly Desk tick happens to succeed.

    This is the same final materialization step the Desk tick performs. It is
    intentionally a no-op outside the active Desk window, when there is no
    homepage snapshot to refresh.
    """
    result = await build_desk_render_variants(db)
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


async def main() -> None:
    async with SessionLocal() as db:
        async with db.begin():
            summary = await rebuild(db)
            refreshed_snapshots = await _materialize_desk_render_snapshots(db)
    print(
        f"Rebuilt SL metrics: {summary['seasons']} player-seasons, "
        f"{summary['contexts']} contexts ({summary['adv_pools']} ADV-eligible); "
        f"refreshed {refreshed_snapshots} Desk render snapshots."
    )
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
