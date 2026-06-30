r"""Seed ``player_external_ids(system='nba_stats')`` from resolved SL players.

Every resolved ``SummerLeagueSourcePlayer`` already carries both a canonical
``player_id`` and an NBA Stats ``PERSON_ID``. This sweep promotes that pair into
the canonical external-id table so that future Summer League resolution is
deterministic (an O(1) PERSON_ID lookup rather than a fuzzy name match) and so
C1 headshot URLs can join ``players_master`` -> external id -> the NBA CDN.

The sweep is idempotent: re-running over unchanged data inserts nothing. Run it
now over the existing multi-year resolved cohort, then re-run after each 2026
roster/box-score resolution pass to pick up the rookie class.

Usage::

    export DATABASE_URL="postgresql+asyncpg://..."
    conda run -n draftguru --no-capture-output \
        python scripts/seed_nba_stats_external_ids.py

Pass ``--dry-run`` to report the would-be seed counts without writing.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Sequence

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.services.summer_league.player_resolution import (
    ExternalIdBackfillReport,
    backfill_nba_stats_external_ids,
)
from app.utils.db_async import _prepare_asyncpg_connection

load_dotenv()


async def _run(*, dry_run: bool = False, verbose: bool = False) -> None:
    """Run the external-id backfill sweep.

    Args:
        dry_run: If ``True``, roll back after reporting the would-be counts.
        verbose: Print one line per detected conflict.
    """
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        sys.exit(1)

    normalized_url, connect_args = _prepare_asyncpg_connection(db_url)
    engine = create_async_engine(normalized_url, connect_args=connect_args)
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    report: ExternalIdBackfillReport
    async with session_factory() as session:
        report = await backfill_nba_stats_external_ids(session)
        if dry_run:
            await session.rollback()
        else:
            await session.commit()

    await engine.dispose()

    action = "Would seed" if dry_run else "Seeded"
    print(
        f"{action}: seeded={report.seeded}  "
        f"already_present={report.already_present}  "
        f"conflicts={len(report.conflicts)}",
        flush=True,
    )
    if report.conflicts and (verbose or not dry_run):
        print("Conflicts (person_id -> existing_player / attempted_player):")
        for person_id, existing_player, attempted_player in report.conflicts:
            print(
                f"  {person_id}: existing={existing_player} "
                f"attempted={attempted_player}",
                flush=True,
            )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report would-be seed counts without writing to the database",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print one line per detected conflict",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Command-line arguments; uses ``sys.argv[1:]`` when ``None``.

    Returns:
        Exit code: 0 on success, 1 on error.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    asyncio.run(_run(dry_run=args.dry_run, verbose=args.verbose))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
