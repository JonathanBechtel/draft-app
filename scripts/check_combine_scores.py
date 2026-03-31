"""Quick verification script for combine score data."""

import asyncio
import json

from app.utils.db_async import SessionLocal, load_schema_modules
from sqlalchemy import text


async def check():
    load_schema_modules()
    async with SessionLocal() as db:
        rows = await db.execute(
            text(
                "SELECT pm.display_name, pmv.raw_value, pmv.rank, pmv.percentile "
                "FROM player_metric_values pmv "
                "JOIN metric_definitions md ON md.id = pmv.metric_definition_id "
                "JOIN metric_snapshots ms ON ms.id = pmv.snapshot_id "
                "JOIN players_master pm ON pm.id = pmv.player_id "
                "WHERE md.metric_key = 'combine_score_overall' "
                "AND ms.position_scope_parent IS NULL "
                "AND ms.id = 2207 "
                "ORDER BY pmv.percentile DESC LIMIT 10"
            )
        )
        print("Top 10 Overall Combine Scores (2025-26):")
        for r in rows:
            name, raw, rank, pctl = r
            print(f"  {name:30s}  z={raw:+.3f}  rank={rank}  pctl={pctl:.1f}")

        detail = await db.execute(
            text(
                "SELECT pm.display_name, pmv.extra_context "
                "FROM player_metric_values pmv "
                "JOIN metric_definitions md ON md.id = pmv.metric_definition_id "
                "JOIN players_master pm ON pm.id = pmv.player_id "
                "WHERE md.metric_key = 'combine_score_overall' "
                "AND pmv.snapshot_id = 2207 "
                "ORDER BY pmv.percentile DESC LIMIT 1"
            )
        )
        row = detail.first()
        if row:
            print("")
            print(f"Component breakdown for {row[0]}:")
            print(json.dumps(row[1], indent=2))


asyncio.run(check())
