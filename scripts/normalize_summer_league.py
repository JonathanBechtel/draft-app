"""Normalize audited Summer League competitions, teams, games, and player logs."""

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
    normalize_pbp_events,
    normalize_player_game_logs,
    normalize_shot_events,
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
    parser.add_argument(
        "--include-player-logs",
        action="store_true",
        help="Also normalize source players and player game logs after games.",
    )
    parser.add_argument(
        "--player-logs-only",
        action="store_true",
        help="Only normalize source players and player game logs.",
    )
    parser.add_argument(
        "--include-shot-events",
        action="store_true",
        help="Also parse shotchartdetail snapshots into shot events after games.",
    )
    parser.add_argument(
        "--include-pbp-events",
        action="store_true",
        help="Also parse playbyplayv2 snapshots into PBP events after games.",
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
            report = None
            if not args.player_logs_only:
                report = await normalize_competition_games(
                    db,
                    year=args.year,
                    league_id=args.league_id,
                    raw_root=args.raw_root,
                )
            player_report = None
            if args.include_player_logs or args.player_logs_only:
                player_report = await normalize_player_game_logs(
                    db,
                    year=args.year,
                    league_id=args.league_id,
                    raw_root=args.raw_root,
                )
            shot_report = None
            if args.include_shot_events and not args.player_logs_only:
                shot_report = await normalize_shot_events(
                    db,
                    year=args.year,
                    league_id=args.league_id,
                    raw_root=args.raw_root,
                )
            pbp_report = None
            if args.include_pbp_events and not args.player_logs_only:
                pbp_report = await normalize_pbp_events(
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
    messages: list[str] = []
    if report is not None:
        messages.append(
            f"{report.year}/{report.league_id}: teams={report.teams_upserted} "
            f"games={report.games_upserted} "
            f"team_logs={report.team_game_logs_upserted} "
            f"quality={report.data_quality.value}"
        )
    if player_report is not None:
        messages.append(
            f"{player_report.year}/{player_report.league_id}: "
            f"source_players={player_report.source_players_upserted} "
            f"player_logs={player_report.player_game_logs_upserted} "
            f"skipped_player_logs={player_report.player_game_logs_skipped}"
        )
    if shot_report is not None:
        messages.append(
            f"{shot_report.year}/{shot_report.league_id}: "
            f"shot_events={shot_report.shot_events_upserted} "
            f"games_processed={shot_report.games_processed} "
            f"games_with_shots={shot_report.games_with_shots}"
        )
    if pbp_report is not None:
        messages.append(
            f"{pbp_report.year}/{pbp_report.league_id}: "
            f"pbp_events={pbp_report.pbp_events_upserted} "
            f"games_processed={pbp_report.games_processed} "
            f"games_with_pbp={pbp_report.games_with_pbp}"
        )
    print("\n".join(messages), flush=True)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and run the async normalizer."""
    parser = build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(run_normalization(args))


if __name__ == "__main__":
    raise SystemExit(main())
