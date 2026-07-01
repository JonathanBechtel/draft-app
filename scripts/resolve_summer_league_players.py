"""Resolve Summer League source players to canonical DraftGuru players.

Run:

    conda run -n draftguru python scripts/resolve_summer_league_players.py \
      --year 2024 --league-id 15
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
load_dotenv()

from app.config import settings  # noqa: E402
from app.services.summer_league.player_resolution import (  # noqa: E402
    SummerLeagueResolutionReport,
    resolve_summer_league_players,
)


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", type=int, help="Optional Summer League year filter")
    parser.add_argument("--league-id", help="Optional NBA.com LeagueID filter")
    parser.add_argument(
        "--create-stubs",
        action="store_true",
        help=(
            "Create PlayerMaster stubs only for unresolved source players with no "
            "serious candidate."
        ),
    )
    return parser


def summarize_resolution_report(report: SummerLeagueResolutionReport) -> str:
    """Return a compact human-readable report summary."""
    scope = "all"
    if report.year is not None and report.league_id is not None:
        scope = f"{report.year}/{report.league_id}"
    elif report.year is not None:
        scope = str(report.year)
    elif report.league_id is not None:
        scope = f"league_id={report.league_id}"

    return (
        f"{scope}: total={report.total_source_players} "
        f"resolved={report.resolved_source_players} "
        f"unresolved={report.unresolved_source_players} "
        f"external_id={report.external_id_resolutions} "
        f"existing_source={report.existing_source_resolutions} "
        f"exact={report.exact_resolutions} "
        f"alias={report.alias_resolutions} "
        f"candidate={report.candidate_source_players} "
        f"stubs={report.stubs_created} "
        f"logs_backfilled={report.player_game_logs_backfilled} "
        f"participations_backfilled={report.participation_rows_backfilled} "
        f"shots_backfilled={report.shot_events_backfilled}"
    )


async def run_resolution(args: argparse.Namespace) -> int:
    """Run the player-resolution command and return a process exit code."""
    database_url = os.getenv("DATABASE_URL") or settings.database_url
    if not database_url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 1

    engine = create_async_engine(database_url, echo=False)
    session_factory = async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    try:
        async with session_factory() as db:
            report = await resolve_summer_league_players(
                db,
                year=args.year,
                league_id=args.league_id,
                create_stubs=args.create_stubs,
            )
            await db.commit()
    finally:
        await engine.dispose()

    print(summarize_resolution_report(report), flush=True)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and run the async resolution command."""
    parser = build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(run_resolution(args))


if __name__ == "__main__":
    raise SystemExit(main())
