"""Build and publish the materialized Summer League advanced metrics.

Builds an inactive dated version from the raw box logs, prepares the Desk render
variants against that version, then flips both read pointers atomically.

Run:
  scripts/with-db-env.sh conda run -n draftguru python scripts/rebuild_sl_metrics.py
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.services.event_desk.render_snapshots import upsert_render_snapshots
from app.services.event_desk.snapshot_materialization import (
    prepare_desk_render_snapshots,
)
from app.services.sources.summer_league.metric_publish import publish_metric_version
from app.services.sources.summer_league.metrics import (
    rebuild_staged,
    set_repeatable_read_snapshot,
)
from app.services.ingest.write_lock import (
    acquire_summer_league_writer_lock_bounded,
)
from app.utils.db_async import SessionLocal, engine

# A manual rebuild racing a scheduled cron writer (the Desk backbone class,
# or another manual run) previously took the unbounded blocking acquire,
# which could hang indefinitely behind a long-running holder with no
# feedback. The bounded acquire fails loudly with a clear timeout instead --
# the same pattern the Desk tick classes use (see
# `app/services/sources/summer_league/desk_tick/shared.py`).
DEFAULT_REBUILD_LOCK_MAX_WAIT_SECONDS = 30.0


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
            await acquire_summer_league_writer_lock_bounded(
                db, max_wait_seconds=DEFAULT_REBUILD_LOCK_MAX_WAIT_SECONDS
            )
            publication_kwargs: dict[str, Any] = {
                "version": int(summary["version"]),
                "model_version": str(summary["model_version"]),
            }
            if summary.get("effective_day") is not None:
                publication_kwargs["effective_day"] = summary["effective_day"]
            skipped_competition_ids = await publish_metric_version(
                db,
                **publication_kwargs,
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
