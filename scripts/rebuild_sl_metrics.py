"""Build and publish the materialized Summer League advanced metrics.

Builds an inactive dated version from the raw box logs, prepares the Desk render
variants against that version, then flips both read pointers atomically.

Run:
  scripts/with-db-env.sh conda run -n draftguru python scripts/rebuild_sl_metrics.py
"""

from __future__ import annotations

import asyncio

from app.services.event_desk.render_snapshots import upsert_render_snapshots
from app.services.event_desk.snapshot_materialization import (
    prepare_desk_render_snapshots,
)
from app.services.summer_league.metric_publish import publish_metric_version
from app.services.summer_league.metrics import (
    rebuild_staged,
    set_repeatable_read_snapshot,
)
from app.services.summer_league.write_lock import acquire_summer_league_writer_lock
from app.utils.db_async import SessionLocal, engine


async def main() -> None:
    async with SessionLocal() as db:
        async with db.begin():
            await set_repeatable_read_snapshot(db)
            summary = await rebuild_staged(db)
        async with db.begin():
            snapshot_writes = await prepare_desk_render_snapshots(
                db, metrics_version=int(summary["version"])
            )
        async with db.begin():
            await acquire_summer_league_writer_lock(db)
            skipped_competition_ids = await publish_metric_version(
                db,
                version=int(summary["version"]),
                model_version=str(summary["model_version"]),
            )
            if skipped_competition_ids:
                snapshot_writes = []
            else:
                await upsert_render_snapshots(db, snapshot_writes)
    print(
        f"Rebuilt SL metrics: {summary['seasons']} player-seasons, "
        f"{summary['contexts']} contexts ({summary['adv_pools']} ADV-eligible); "
        f"refreshed {len(snapshot_writes)} Desk render snapshots "
        f"(version {summary['version']})."
    )
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
