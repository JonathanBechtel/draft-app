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
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from app.services.college_stats_service import run_college_stats_sweep
from app.utils.db_async import SessionLocal, dispose_engine


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
        "--throttle",
        type=float,
        default=3.0,
        help="Seconds to sleep between live HTTP requests (default: 3.0)",
    )
    parser.add_argument(
        "--cache-dir",
        type=str,
        default="scraper/cache/players",
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
        )

        print(
            f"\nSummary: {result.players_attempted} attempted, "
            f"{result.players_scraped} scraped, "
            f"{result.players_skipped} skipped, "
            f"{result.players_failed} failed, "
            f"{result.seasons_upserted} seasons upserted"
        )

        if result.errors:
            print(f"\nErrors ({len(result.errors)}):")
            for err in result.errors:
                print(f"  - {err}")

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
