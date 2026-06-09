"""Normalize audited Summer League competitions, teams, games, and team logs."""

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
from app.services.summer_league.normalization import (  # noqa: E402
    normalize_competition_games,
)


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", required=True, type=int)
    parser.add_argument("--league-id", required=True)
    parser.add_argument(
        "--raw-root",
        type=Path,
        default=Path("data/raw/nba_stats/summer_league"),
    )
    return parser


async def run_normalization(args: argparse.Namespace) -> int:
    """Run the normalization command."""
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
            report = await normalize_competition_games(
                db,
                year=args.year,
                league_id=args.league_id,
                raw_root=args.raw_root,
            )
            await db.commit()
    except (FileNotFoundError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        await engine.dispose()
    print(
        f"{report.year}/{report.league_id}: teams={report.teams_upserted} "
        f"games={report.games_upserted} team_logs={report.team_game_logs_upserted} "
        f"quality={report.data_quality.value}",
        flush=True,
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and run the async normalizer."""
    parser = build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(run_normalization(args))


if __name__ == "__main__":
    raise SystemExit(main())
