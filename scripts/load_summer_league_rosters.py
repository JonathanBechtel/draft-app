r"""Load a raw Summer League roster snapshot into the canonical foundation tables.

Reads the JSON snapshot written by ``scripts/fetch_summer_league_rosters.py``
and calls ``load_roster_snapshot`` to upsert source players, append-only
affiliation assertions, and participation bridge rows.  The load is idempotent:
re-running against an unchanged snapshot creates no new rows.

Usage::

    export DATABASE_URL="postgresql+asyncpg://..."
    conda run -n draftguru --no-capture-output \
        python scripts/load_summer_league_rosters.py --year 2026 --league-id 15

Pass ``--dry-run`` to parse and report the diff without writing to the database.
Pass ``--snapshot`` to provide an explicit snapshot path instead of the default
location (``data/raw/nba_stats/summer_league/{year}/{league_id}/rosters.json``).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.services.summer_league.endpoints import (
    SUPPORTED_SUMMER_LEAGUES,
    normalize_league_id,
    normalize_season,
)
from app.services.summer_league.raw_store import SummerLeagueRawStore
from app.services.summer_league.roster_ingest import (
    CompetitionKey,
    RosterDiffReport,
    load_roster_snapshot,
)
from app.services.summer_league.roster_parse import RosterEntry
from app.utils.db_async import _prepare_asyncpg_connection, load_schema_modules

load_dotenv()


# ---------------------------------------------------------------------------
# Snapshot parsing
# ---------------------------------------------------------------------------


def _entries_from_snapshot(snapshot: dict[str, Any]) -> list[RosterEntry]:
    """Reconstruct ``RosterEntry`` objects from a roster snapshot dictionary.

    Args:
        snapshot: JSON object as returned by ``fetch_summer_league_rosters.py``.

    Returns:
        Flat list of ``RosterEntry`` instances across all teams.
    """
    entries: list[RosterEntry] = []
    for team in snapshot.get("teams", []):
        for player in team.get("players", []):
            if not isinstance(player, dict):
                continue
            entries.append(
                RosterEntry(
                    nba_stats_person_id=str(player.get("nba_stats_person_id") or ""),
                    raw_player_name=str(player.get("raw_player_name") or ""),
                    team_id=str(player.get("team_id") or ""),
                    jersey=player.get("jersey"),
                    position=player.get("position"),
                    height=player.get("height"),
                    weight=player.get("weight"),
                    birth_date=player.get("birth_date"),
                    school=player.get("school"),
                    how_acquired=player.get("how_acquired"),
                    league_id=str(player.get("league_id") or ""),
                )
            )
    return entries


# ---------------------------------------------------------------------------
# Main async function
# ---------------------------------------------------------------------------


async def _run(
    *,
    year: int,
    league_id: str,
    snapshot_path: Path | None = None,
    dry_run: bool = False,
    verbose: bool = False,
) -> None:
    """Load a roster snapshot into the database.

    Args:
        year: Summer League season year.
        league_id: Normalized NBA Stats LeagueID (``"15"``, ``"13"``, ``"16"``).
        snapshot_path: Explicit path to the rosters.json file; if ``None`` the
            default raw-store location is used.
        dry_run: If ``True``, roll back after reporting the diff.
        verbose: Print per-team diff lines.
    """
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        sys.exit(1)

    # Register every SQLModel table so cross-table FKs (e.g.
    # summer_league_team_entries.nba_team_id -> nba_teams) resolve when the
    # loader's models are mapped; the loader's own imports don't pull nba_teams.
    load_schema_modules()

    # Resolve the snapshot path.
    store = SummerLeagueRawStore()
    resolved_path: Path = (
        snapshot_path
        if snapshot_path is not None
        else store.season_file(year=year, league_id=league_id, name="rosters")
    )

    if not resolved_path.exists():
        print(f"ERROR: snapshot not found: {resolved_path}", file=sys.stderr)
        sys.exit(1)

    snapshot = store.read_json(resolved_path)
    entries = _entries_from_snapshot(snapshot)

    season = normalize_season(year)
    venue = SUPPORTED_SUMMER_LEAGUES[league_id]
    competition_key = CompetitionKey(
        year=int(season),
        league_id=league_id,
        venue_slug=venue.slug,
    )

    recorded_at = datetime.now(timezone.utc).replace(tzinfo=None)

    if verbose:
        print(
            f"Loading {len(entries)} players into {competition_key.year} "
            f"{competition_key.venue_slug} (league_id={competition_key.league_id})",
            flush=True,
        )

    normalized_url, connect_args = _prepare_asyncpg_connection(db_url)
    engine = create_async_engine(normalized_url, connect_args=connect_args)
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    report: RosterDiffReport
    async with session_factory() as session:
        report = await load_roster_snapshot(
            session,
            competition_key,
            entries,
            recorded_at=recorded_at,
        )
        if dry_run:
            await session.rollback()
        else:
            await session.commit()

    await engine.dispose()

    # Print summary.
    action = "Would write" if dry_run else "Wrote"
    print(
        f"{action}: added={report.added}  unchanged={report.unchanged}  "
        f"cut={report.cut}",
        flush=True,
    )
    if verbose:
        for team_id, diff in sorted(report.per_team.items()):
            print(
                f"  team {team_id}: "
                f"added={diff.added} unchanged={diff.unchanged} cut={diff.cut}",
                flush=True,
            )


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
        help="Summer League year (e.g. 2026)",
    )
    parser.add_argument(
        "--league-id",
        required=True,
        help="NBA Stats LeagueID: 15=Las Vegas, 13=California, 16=SLC",
    )
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=None,
        help="Explicit path to rosters.json; defaults to raw-store location",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and report diff without writing to the database",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-team diff lines",
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

    try:
        league_id = normalize_league_id(args.league_id)
    except ValueError as exc:
        parser.error(str(exc))

    asyncio.run(
        _run(
            year=args.year,
            league_id=league_id,
            snapshot_path=args.snapshot,
            dry_run=args.dry_run,
            verbose=args.verbose,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
