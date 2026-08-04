r"""Print a read-only announced-vs-played reconcile report for a Summer League.

Wraps ``reconcile_competition`` (``app/services/summer_league/roster_reconcile.py``)
in a CLI: resolves ``--year``/``--league-id`` to the matching
``SummerLeagueEdition`` row(s), runs the reconcile, and prints totals plus
the two flagged lists (announced-but-never-played, played-but-never-announced).

This script issues only ``SELECT`` statements — it never writes to the database.

Usage::

    export DATABASE_URL="postgresql+asyncpg://..."
    conda run -n draftguru --no-capture-output \
        python scripts/reconcile_summer_league_rosters.py --year 2025 --league-id 13

Pass ``--all-venues`` instead of ``--league-id`` to reconcile every supported
Summer League venue for the given year, skipping any venue with no matching
competition row.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Sequence

from dotenv import load_dotenv
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.schemas.summer_league import SummerLeagueEdition
from app.services.sources.summer_league.endpoints import (
    SUPPORTED_SUMMER_LEAGUES,
    normalize_league_id,
    normalize_season,
)
from app.services.sources.summer_league.roster_reconcile import (
    ReconcileEntry,
    RosterReconcileReport,
    reconcile_competition,
)
from app.utils.db_async import _prepare_asyncpg_connection, load_schema_modules

load_dotenv()


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def _print_entries(label: str, entries: list[ReconcileEntry]) -> None:
    """Print one labeled section of flagged reconcile entries.

    Args:
        label: Section heading (e.g. ``"Announced, never played"``).
        entries: Flagged entries to print, one per line.
    """
    print(f"  {label} ({len(entries)}):")
    for entry in entries:
        print(f"    - {entry.name or '(unknown)'} [{entry.team_name}]", flush=True)


def _print_report(report: RosterReconcileReport, *, display_name: str) -> None:
    """Print a full reconcile report for one competition.

    Args:
        report: Reconcile result from ``reconcile_competition``.
        display_name: Human-readable competition label for the header line.
    """
    print(f"\n{display_name} (competition_id={report.competition_id})")
    print(
        f"  total_announced={report.total_announced}  "
        f"total_played={report.total_played}  "
        f"announced_and_played={report.announced_and_played}",
        flush=True,
    )
    _print_entries("Announced, never played", report.announced_not_played)
    _print_entries("Played, never announced", report.played_not_announced)


# ---------------------------------------------------------------------------
# Main async function
# ---------------------------------------------------------------------------


async def _run(*, year: int, league_ids: list[str]) -> int:
    """Reconcile and print a report for each requested competition.

    Args:
        year: Summer League season year.
        league_ids: Normalized NBA Stats LeagueIDs to reconcile.

    Returns:
        Exit code: 0 if at least one competition was found and reconciled,
        1 if none of the requested competitions exist in the database.
    """
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        return 1

    # Register every SQLModel table so relationships resolve correctly
    # regardless of which schemas this script's own imports happen to pull in.
    load_schema_modules()

    season = normalize_season(year)
    normalized_url, connect_args = _prepare_asyncpg_connection(db_url)
    engine = create_async_engine(normalized_url, connect_args=connect_args)
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    found_any = False
    async with session_factory() as session:
        for league_id in league_ids:
            venue = SUPPORTED_SUMMER_LEAGUES[league_id]
            result = await session.execute(
                select(SummerLeagueEdition).where(
                    SummerLeagueEdition.year == int(season),  # type: ignore[arg-type]
                    SummerLeagueEdition.league_id == league_id,  # type: ignore[arg-type]
                )
            )
            competition = result.scalar_one_or_none()
            if competition is None or competition.id is None:
                print(
                    f"No competition found for year={season} "
                    f"league_id={league_id} ({venue.display_name})",
                    flush=True,
                )
                continue

            found_any = True
            report = await reconcile_competition(session, competition.id)
            _print_report(report, display_name=competition.display_name)

    await engine.dispose()

    return 0 if found_any else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--year",
        required=True,
        type=int,
        help="Summer League year (e.g. 2025)",
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--league-id",
        help="NBA Stats LeagueID: 15=Las Vegas, 13=California, 16=SLC, 14=Orlando",
    )
    target.add_argument(
        "--all-venues",
        action="store_true",
        help="Reconcile every supported Summer League venue for --year",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Command-line arguments; uses ``sys.argv[1:]`` when ``None``.

    Returns:
        Exit code: 0 on success, 1 on error or if no competition was found.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.all_venues:
        league_ids = sorted(SUPPORTED_SUMMER_LEAGUES)
    else:
        try:
            league_ids = [normalize_league_id(args.league_id)]
        except ValueError as exc:
            parser.error(str(exc))
            return 2  # pragma: no cover - argparse.error() exits the process

    return asyncio.run(_run(year=args.year, league_ids=league_ids))


if __name__ == "__main__":
    raise SystemExit(main())
