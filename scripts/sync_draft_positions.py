#!/usr/bin/env python
r"""Backfill ``players_master.draft_*`` from resolved ``draft_results`` picks.

``ingest_draft_results.py`` runs this automatically at the end of every ingest,
so you normally do not need it. Use it as a standalone catch-up when picks were
loaded by an ingest that predates the auto-sync (e.g. a parallel worktree), or
to re-propagate after fixing a name resolution — it is idempotent.

Usage::

    # sync a single class
    scripts/with-db-env.sh conda run -n draftguru --no-capture-output \\
        python scripts/sync_draft_positions.py --draft-year 2026

    # sync every year present in draft_results
    scripts/with-db-env.sh conda run -n draftguru --no-capture-output \\
        python scripts/sync_draft_positions.py
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Optional

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.services.draft_position_sync_service import sync_draft_positions
from app.utils.db_async import _prepare_asyncpg_connection

load_dotenv()


async def run(draft_year: Optional[int]) -> None:
    """Open a session, sync draft positions, and report the row count."""
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        sys.exit(1)

    normalized_url, connect_args = _prepare_asyncpg_connection(db_url)
    engine = create_async_engine(normalized_url, connect_args=connect_args)
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    async with session_factory() as session:
        updated = await sync_draft_positions(session, draft_year=draft_year)
        await session.commit()

    await engine.dispose()

    scope = f"draft year {draft_year}" if draft_year is not None else "all years"
    print(f"Synced draft position onto {updated} players_master rows ({scope}).")


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--draft-year",
        type=int,
        default=None,
        help="Restrict to one draft year; omit to sync every year.",
    )
    args = parser.parse_args()
    asyncio.run(run(args.draft_year))


if __name__ == "__main__":
    main()
