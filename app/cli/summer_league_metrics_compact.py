"""Run the standalone daily Summer League metric-version compaction job.

Run:
  python -m app.cli.summer_league_metrics_compact
"""

from __future__ import annotations

import asyncio

from app.services.summer_league.metric_compaction import compact_metric_versions
from app.utils.db_async import SessionLocal, engine


async def main() -> None:
    """Compact superseded closed-day metric projections and report the result."""
    try:
        async with SessionLocal() as db:
            async with db.begin():
                summary = await compact_metric_versions(db)
        print(
            "Compacted Summer League metric versions: "
            f"{summary.context_rows_deleted} contexts, "
            f"{summary.season_rows_deleted} player-seasons "
            f"(cutoff {summary.cutoff.isoformat()})."
        )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
