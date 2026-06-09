"""Backfill audited Summer League backbone data into product tables.

Run:

    conda run -n draftguru python scripts/backfill_summer_league_backbone.py \
      --year 2024 --league-id 15 \
      --raw-root data/raw/nba_stats/summer_league
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
load_dotenv()

from app.services.summer_league.backfill import (  # noqa: E402
    SummerLeagueBackfillOptions,
    backfill_summer_league_backbone,
    summarize_backfill_report,
    write_backfill_report,
)


def parse_non_negative_int(value: str) -> int:
    """Parse a non-negative integer for argparse."""
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Expected an integer, got {value!r}") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("Value must be non-negative")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", required=True, type=int)
    parser.add_argument("--league-id", required=True)
    parser.add_argument(
        "--raw-root",
        type=Path,
        default=Path("data/raw/nba_stats/summer_league"),
        help="Local Summer League raw snapshot root",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Continue after audit parse failures when later stages can proceed.",
    )
    parser.add_argument("--limit-games", type=parse_non_negative_int)
    parser.add_argument(
        "--create-stubs",
        action="store_true",
        help="Create canonical player stubs for unresolved no-candidate players.",
    )
    parser.add_argument(
        "--report-path",
        type=Path,
        help="Optional JSON report output path",
    )
    return parser


def load_database_url() -> str | None:
    """Resolve the database URL without breaking --help in unconfigured shells."""
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        return database_url
    try:
        from app.config import settings  # noqa: PLC0415
    except Exception:
        return None
    return settings.database_url


async def run_backfill(args: argparse.Namespace) -> int:
    """Run the Summer League backbone backfill command."""
    database_url = load_database_url()
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
            options = SummerLeagueBackfillOptions(
                year=args.year,
                league_id=args.league_id,
                raw_root=args.raw_root,
                dry_run=args.dry_run,
                force=args.force,
                limit_games=args.limit_games,
                create_stubs=args.create_stubs,
            )
            report = await backfill_summer_league_backbone(db, options)
            if args.dry_run:
                await db.rollback()
            else:
                await db.commit()
    except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        await engine.dispose()

    if args.report_path is not None:
        write_backfill_report(report, args.report_path)
    print(summarize_backfill_report(report), flush=True)
    return 1 if report.stopped_after_stage is not None else 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and run the async backfill command."""
    parser = build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(run_backfill(args))


if __name__ == "__main__":
    raise SystemExit(main())
