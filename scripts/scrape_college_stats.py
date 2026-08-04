"""CLI wrapper for college stats scraping from Basketball-Reference.

Usage:
    python scripts/scrape_college_stats.py [options]

Examples:
    # Dry-run a single player
    python scripts/scrape_college_stats.py --player-id 42 --dry-run --verbose

    # Backfill first 10 players
    python scripts/scrape_college_stats.py --limit 10 --verbose

    # Full backfill, re-fetching cached pages
    python scripts/scrape_college_stats.py --refresh

    # Cron-style: only players missing sports_reference stats
    python scripts/scrape_college_stats.py --only-missing

    # Target just the Summer League rostered cohort (a given year/venue)
    python scripts/scrape_college_stats.py --sl-cohort --sl-year 2025
    python scripts/scrape_college_stats.py --sl-cohort --sl-league-id 13 --only-missing
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Ensure the co-located repo (this script's own checkout) wins over any
# editable/site-package install of ``app`` — otherwise a stale install can
# shadow newer code (e.g. run_college_stats_sweep's sl_cohort kwarg) and raise
# a confusing TypeError. Mirrors the bootstrap in bbref_bio_scraper.py.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.college_stats_service import run_college_stats_sweep  # noqa: E402
from app.utils.db_async import SessionLocal, dispose_engine  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Scrape college basketball stats from Basketball-Reference"
    )
    parser.add_argument(
        "--player-id",
        type=int,
        default=None,
        help="Process a single player by ID",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max number of players to process",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and print stats without writing to DB",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Re-fetch cached HTML pages from BBRef",
    )
    parser.add_argument(
        "--only-missing",
        action="store_true",
        help="Only process players without existing sports_reference stats",
    )
    parser.add_argument(
        "--sl-cohort",
        action="store_true",
        help=(
            "Restrict the target set to the Summer League rostered cohort "
            "(app.services.sources.summer_league.cohort) instead of all players. "
            "Cohort players missing a school + BBRef id (e.g. non-NCAA/"
            "international) are reported as no-source, not failed."
        ),
    )
    parser.add_argument(
        "--sl-year",
        type=int,
        default=None,
        help="Restrict the SL cohort to a competition year (requires --sl-cohort)",
    )
    parser.add_argument(
        "--sl-league-id",
        type=str,
        default=None,
        help="Restrict the SL cohort to an NBA.com LeagueID (requires --sl-cohort)",
    )
    parser.add_argument(
        "--sl-venue-slug",
        type=str,
        default=None,
        help="Restrict the SL cohort to a venue slug (requires --sl-cohort)",
    )
    parser.add_argument(
        "--throttle",
        type=float,
        default=3.0,
        help="Seconds to sleep between live HTTP requests (default: 3.0)",
    )
    parser.add_argument(
        "--cache-dir",
        type=str,
        default="data/scraper-cache/players",
        help="Cache directory for HTML files",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug-level logging",
    )
    return parser


async def _main(args: argparse.Namespace) -> int:
    try:
        result = await run_college_stats_sweep(
            SessionLocal,
            limit=args.limit,
            player_id=args.player_id,
            dry_run=args.dry_run,
            refresh=args.refresh,
            throttle=args.throttle,
            cache_dir=Path(args.cache_dir),
            only_missing=args.only_missing,
            sl_cohort=args.sl_cohort,
            sl_year=args.sl_year,
            sl_league_id=args.sl_league_id,
            sl_venue_slug=args.sl_venue_slug,
        )

        print(
            f"\nSummary: {result.players_attempted} attempted, "
            f"{result.players_scraped} scraped, "
            f"{result.players_skipped} skipped, "
            f"{result.players_failed} failed, "
            f"{result.seasons_upserted} seasons upserted, "
            f"{len(result.no_source)} no-source"
        )

        if result.errors:
            print(f"\nErrors ({len(result.errors)}):")
            for err in result.errors:
                print(f"  - {err}")

        if result.no_source:
            print(f"\nNo source ({len(result.no_source)}):")
            for entry in result.no_source:
                print(f"  - {entry}")

        return 1 if result.players_failed > 0 else 0

    finally:
        await dispose_engine()


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    exit_code = asyncio.run(_main(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
