"""Recompute and persist the materialized Summer League advanced metrics.

Wipes and repopulates ``summer_league_metric_models`` / ``_metric_contexts`` /
``_player_seasons`` from the raw box logs. Safe to re-run; it is a full rebuild.

Run:
  scripts/with-db-env.sh conda run -n draftguru python scripts/rebuild_sl_metrics.py
"""

from __future__ import annotations

import asyncio

from app.services.event_desk.snapshot_materialization import (
    materialize_desk_render_snapshots,
)
from app.services.summer_league.metrics import rebuild
from app.utils.db_async import SessionLocal, engine


async def main() -> None:
    async with SessionLocal() as db:
        async with db.begin():
            summary = await rebuild(db)
            refreshed_snapshots = await materialize_desk_render_snapshots(db)
    print(
        f"Rebuilt SL metrics: {summary['seasons']} player-seasons, "
        f"{summary['contexts']} contexts ({summary['adv_pools']} ADV-eligible); "
        f"refreshed {refreshed_snapshots} Desk render snapshots."
    )
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
