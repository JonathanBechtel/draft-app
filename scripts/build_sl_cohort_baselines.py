"""Job A — build/refresh the Summer League Desk cohort baselines (T1).

Offline, rare job (`docs/plans/summer-league-scouts-desk-behavior-spec.md`
§6, §10): recomputes ``summer_league_cohort_baselines`` from 2017-2025 SL
history (draft slot + ``summer_league_player_seasons``) and activates a new
``baseline_version``. Safe to re-run — each run writes a fresh version and
flips ``is_active``; it never mutates or deletes prior versions' rows. The
hourly tick (Job B, a separate ticket) only ever reads the active version —
this job never runs on that path.

Run:
  scripts/with-db-env.sh conda run -n draftguru python scripts/build_sl_cohort_baselines.py
  scripts/with-db-env.sh conda run -n draftguru python scripts/build_sl_cohort_baselines.py \
      --season-range 2017-2025 --min-minutes 40
"""

from __future__ import annotations

import argparse
import asyncio

from app.services.summer_league.cohort_baselines import (
    DEFAULT_GAME_MIN_MINUTES,
    DEFAULT_MIN_MINUTES,
    DEFAULT_SEASON_RANGE,
    build_baselines,
)
from app.utils.db_async import SessionLocal, engine


async def main(season_range: str, min_minutes: float, game_min_minutes: float) -> None:
    async with SessionLocal() as db:
        async with db.begin():
            version = await build_baselines(
                db,
                season_range=season_range,
                min_minutes=min_minutes,
                game_min_minutes=game_min_minutes,
            )
    print(
        f"Built Summer League cohort baselines: version={version} "
        f"(season_range={season_range}, min_minutes={min_minutes}, "
        f"game_min_minutes={game_min_minutes})"
    )
    await engine.dispose()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--season-range",
        default=DEFAULT_SEASON_RANGE,
        help="Inclusive '<start>-<end>' year range, e.g. 2017-2025.",
    )
    parser.add_argument(
        "--min-minutes",
        type=float,
        default=DEFAULT_MIN_MINUTES,
        help="Minimum blended minutes for a player-year event to qualify.",
    )
    parser.add_argument(
        "--game-min-minutes",
        type=float,
        default=DEFAULT_GAME_MIN_MINUTES,
        help="Minimum single-game minutes for an individual game to qualify "
        "for the game-grain distribution.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    asyncio.run(main(args.season_range, args.min_minutes, args.game_min_minutes))
