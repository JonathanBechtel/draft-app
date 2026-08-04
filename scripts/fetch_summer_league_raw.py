"""Fetch raw NBA Stats Summer League snapshots.

Run:

    conda run -n draftguru python scripts/fetch_summer_league_raw.py --year 2024 --league-id 15
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from app.services.sources.summer_league.endpoints import (
    normalize_league_id,
    normalize_season,
)
from app.services.sources.summer_league.manifest import SummerLeagueRawManifest
from app.services.sources.summer_league.nba_stats_client import NBAStatsClient
from app.services.sources.summer_league.raw_ingestion import (
    GAME_ENDPOINTS,
    RawIngestionOptions,
    SummerLeagueRawIngestor,
)
from app.services.sources.summer_league.raw_store import SummerLeagueRawStore


@dataclass(frozen=True)
class IngestorContext:
    """Constructed ingestion dependencies for one CLI invocation."""

    ingestor: SummerLeagueRawIngestor
    close: object


def parse_year(value: str) -> int:
    """Parse a four-digit Summer League year for argparse."""
    try:
        season = normalize_season(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    return int(season)


def parse_non_negative_int(value: str) -> int:
    """Parse a non-negative integer for argparse."""
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Expected an integer, got {value!r}") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("Value must be non-negative")
    return parsed


def expand_league_ids(values: Sequence[str]) -> list[str]:
    """Expand repeated and comma-separated LeagueID arguments."""
    league_ids: list[str] = []
    seen: set[str] = set()
    for value in values:
        for part in value.split(","):
            if not part.strip():
                continue
            league_id = normalize_league_id(part)
            if league_id in seen:
                continue
            seen.add(league_id)
            league_ids.append(league_id)
    if not league_ids:
        raise ValueError("At least one --league-id value is required")
    return league_ids


def expand_skip_endpoints(values: Sequence[str] | None) -> tuple[str, ...]:
    """Expand repeated and comma-separated endpoint skip arguments."""
    if not values:
        return ()
    endpoints: list[str] = []
    seen: set[str] = set()
    supported = set(GAME_ENDPOINTS)
    for value in values:
        for part in value.split(","):
            endpoint = part.strip()
            if not endpoint:
                continue
            if endpoint not in supported:
                supported_values = ", ".join(GAME_ENDPOINTS)
                raise ValueError(
                    f"Unsupported endpoint {endpoint!r}; use one of {supported_values}"
                )
            if endpoint in seen:
                continue
            seen.add(endpoint)
            endpoints.append(endpoint)
    return tuple(endpoints)


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", required=True, type=parse_year)
    parser.add_argument(
        "--league-id",
        action="append",
        required=True,
        help="Summer League LeagueID; repeat or comma-separate values",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("data/raw/nba_stats/summer_league"),
        help="Local raw snapshot root",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--delay", type=float, default=0.7)
    parser.add_argument("--retries", type=parse_non_negative_int, default=3)
    parser.add_argument("--retry-delay", type=float, default=2.0)
    parser.add_argument("--limit-games", type=parse_non_negative_int)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--skip-endpoint",
        action="append",
        help="Game detail endpoint to skip; repeat or comma-separate values",
    )
    return parser


def build_ingestor(
    *,
    timeout: float,
    out_dir: Path,
    retries: int,
    retry_delay: float,
    verbose: bool,
) -> IngestorContext:
    """Construct the real client/store/ingestor stack."""
    client = NBAStatsClient(
        timeout=timeout,
        max_retries=retries,
        retry_delay_seconds=retry_delay,
    )
    store = SummerLeagueRawStore(out_dir)
    progress = (lambda message: print(message, flush=True)) if verbose else None
    return IngestorContext(
        ingestor=SummerLeagueRawIngestor(
            client=client,
            store=store,
            progress=progress,
        ),
        close=client.close,
    )


def _print_summary(manifest: SummerLeagueRawManifest) -> None:
    print(
        f"{manifest.year} {manifest.league_id} ({manifest.venue}): "
        f"games={manifest.game_count} "
        f"files_written={len(manifest.files_written)} "
        f"files_skipped={len(manifest.files_skipped)} "
        f"errors={len(manifest.errors)}",
        flush=True,
    )


def _close_context(context: IngestorContext) -> None:
    close = context.close
    if callable(close):
        close()


def main(argv: Sequence[str] | None = None) -> int:
    """Run the raw ingestion CLI."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        league_ids = expand_league_ids(args.league_id)
        skip_endpoints = expand_skip_endpoints(args.skip_endpoint)
    except ValueError as exc:
        parser.error(str(exc))

    context = build_ingestor(
        timeout=args.timeout,
        out_dir=args.out_dir,
        retries=args.retries,
        retry_delay=args.retry_delay,
        verbose=args.verbose,
    )
    successes = 0
    failures = 0
    try:
        for league_id in league_ids:
            options = RawIngestionOptions(
                year=args.year,
                league_id=league_id,
                limit_games=args.limit_games,
                dry_run=args.dry_run,
                force=args.force,
                delay_seconds=args.delay,
                skip_endpoints=skip_endpoints,
            )
            try:
                manifest = context.ingestor.fetch_year_league(options)
            except Exception as exc:
                failures += 1
                print(
                    f"{args.year} {league_id}: failed: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
                continue
            successes += 1
            _print_summary(manifest)
    finally:
        _close_context(context)

    return 1 if failures and successes == 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
