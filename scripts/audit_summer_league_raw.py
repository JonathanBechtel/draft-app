"""Audit local Summer League raw snapshots into database metadata rows.

Run:

    conda run -n draftguru python scripts/audit_summer_league_raw.py \
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

from app.config import settings  # noqa: E402
from app.services.summer_league.audit import (  # noqa: E402
    audit_summer_league_raw,
    summarize_audit_report,
    write_audit_report,
)


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--raw-root",
        type=Path,
        default=Path("data/raw/nba_stats/summer_league"),
        help="Local Summer League raw snapshot root",
    )
    parser.add_argument("--year", type=int, help="Optional Summer League year filter")
    parser.add_argument("--league-id", help="Optional NBA.com LeagueID filter")
    parser.add_argument(
        "--s3-prefix",
        help="Optional durable archive prefix used to populate s3_key fields",
    )
    parser.add_argument(
        "--report-path",
        type=Path,
        help="Optional JSON report output path",
    )
    return parser


async def run_audit(args: argparse.Namespace) -> int:
    """Run the audit command and return a process exit code."""
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
            report = await audit_summer_league_raw(
                db,
                raw_root=args.raw_root,
                year=args.year,
                league_id=args.league_id,
                s3_prefix=args.s3_prefix,
            )
            await db.commit()
    except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        await engine.dispose()

    if args.report_path is not None:
        write_audit_report(report, args.report_path)
    print(summarize_audit_report(report), flush=True)
    return 1 if report.parse_failures else 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and run the async audit command."""
    parser = build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(run_audit(args))


if __name__ == "__main__":
    raise SystemExit(main())
