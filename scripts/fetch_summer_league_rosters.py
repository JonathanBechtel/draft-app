"""Operator CLI for the NBA.com Summer League roster fetch.

Run (pilot venue -- Las Vegas):

    conda run -n draftguru python scripts/fetch_summer_league_rosters.py --year 2026 --league-id 15

Run all venues:

    conda run -n draftguru python scripts/fetch_summer_league_rosters.py --year 2026 --league-id 15,13,16

The fetch/parse/snapshot logic lives in
``app.services.sources.summer_league.roster_fetch`` so the shipped roster cron
(``app/cli/summer_league_roster_runner.py``) and this CLI share one
implementation; this module only handles argument parsing and reporting.

Re-running is idempotent; use --force to overwrite existing snapshots.
Per-team failures are captured and do not abort the run.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from app.services.sources.summer_league.endpoints import (
    normalize_league_id,
    normalize_season,
)
from app.services.sources.summer_league.roster_fetch import (
    RosterFetcher,
    RosterRunResult,
)


def parse_year(value: str) -> int:
    """Parse a four-digit Summer League year for argparse."""
    try:
        season = normalize_season(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    return int(season)


def expand_league_ids(values: Sequence[str]) -> list[str]:
    """Expand repeated and comma-separated LeagueID arguments.

    Args:
        values: Raw ``--league-id`` values (may include commas).

    Returns:
        Deduplicated list of validated LeagueID strings.

    Raises:
        ValueError: If any value is not a supported Summer League ID.
    """
    league_ids: list[str] = []
    seen: set[str] = set()
    for value in values:
        for part in value.split(","):
            if not part.strip():
                continue
            league_id = normalize_league_id(part.strip())
            if league_id in seen:
                continue
            seen.add(league_id)
            league_ids.append(league_id)
    if not league_ids:
        raise ValueError("At least one --league-id value is required")
    return league_ids


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", required=True, type=parse_year)
    parser.add_argument(
        "--league-id",
        action="append",
        required=True,
        help=(
            "Summer League LeagueID (15=Las Vegas, 13=California, 16=SLC); "
            "repeat or comma-separate for multiple venues"
        ),
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("data/raw/nba_stats/summer_league"),
        help="Local raw snapshot root",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--delay",
        type=float,
        default=0.5,
        help="Polite delay in seconds between per-team requests",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing roster snapshot",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and parse but do not write snapshot",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser


def build_fetcher(
    *,
    timeout: float,
    delay: float,
    verbose: bool,
) -> RosterFetcher:
    """Construct a real ``RosterFetcher`` from CLI arguments.

    Args:
        timeout: HTTP request timeout in seconds.
        delay: Polite inter-request delay in seconds.
        verbose: Enable progress output.

    Returns:
        Configured ``RosterFetcher`` instance.
    """
    return RosterFetcher(timeout=timeout, delay_seconds=delay)


def _print_summary(result: RosterRunResult) -> None:
    """Print a one-line run summary."""
    print(
        f"{result.year} {result.league_id} ({result.venue_key}): "
        f"teams={result.team_count} "
        f"players={result.player_count} "
        f"errors={result.error_count}",
        flush=True,
    )
    if result.error:
        print(f"  run-level error: {result.error}", flush=True)
    for team in result.team_results:
        if team.error:
            print(
                f"  team {team.team_id}/{team.slug}: {team.error}",
                flush=True,
            )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the roster-fetch CLI.

    Args:
        argv: Command-line arguments; ``None`` uses ``sys.argv[1:]``.

    Returns:
        Exit code: 0 on full success or partial success, 1 when all runs fail.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        league_ids = expand_league_ids(args.league_id)
    except ValueError as exc:
        parser.error(str(exc))

    fetcher = build_fetcher(
        timeout=args.timeout,
        delay=args.delay,
        verbose=args.verbose,
    )

    successes = 0
    failures = 0
    for league_id in league_ids:
        try:
            result = fetcher.fetch_run(
                year=args.year,
                league_id=league_id,
                out_dir=args.out_dir,
                force=args.force,
                dry_run=args.dry_run,
                verbose=args.verbose,
            )
        except Exception as exc:
            failures += 1
            print(
                f"{args.year} {league_id}: failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            continue

        if result.error and result.team_count == 0:
            # Landing page failed entirely — treat as a run failure
            failures += 1
            print(
                f"{args.year} {league_id}: failed: {result.error}",
                file=sys.stderr,
            )
        else:
            successes += 1
            _print_summary(result)

    return 1 if failures and successes == 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
