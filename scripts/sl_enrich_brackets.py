"""Enrich Summer League games with tournament rounds from the NBA.com schedule.

The ingested game log carries no bracket structure; the ``scheduleleaguev2``
feed does (``gameSubLabel`` = Semifinals / Championship / Consolation). This
fetches the schedule for each stored competition and writes ``round_label`` onto
matching ``summer_league_games`` rows, enabling the venue bracket section.

Run (against whatever DATABASE_URL is configured)::

    scripts/with-db-env.sh conda run -n draftguru python scripts/sl_enrich_brackets.py

Network: stats.nba.com sits behind Akamai bot-management; curl_cffi with Chrome
impersonation reproduces the browser TLS fingerprint and gets through (same
pattern as scripts/probe_summer_league_api.py).
"""

from __future__ import annotations

import argparse
import asyncio
import time

from curl_cffi import requests as cffi
from sqlalchemy import select

from app.schemas.summer_league import SummerLeagueCompetition
from app.services.summer_league.bracket import apply_game_rounds, parse_schedule_rounds
from app.services.summer_league.endpoints import build_schedule_params
from app.utils.db_async import SessionLocal

NBA_API_ROOT = "https://stats.nba.com/stats"
NBA_API_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.nba.com",
    "Referer": "https://www.nba.com/",
    "Accept-Language": "en-US,en;q=0.9",
    "x-nba-stats-origin": "stats",
    "x-nba-stats-token": "true",
}
DELAY = 0.7


def _fetch_schedule(client: "cffi.Session", league_id: str, season: int) -> dict:
    """Fetch one ``scheduleleaguev2`` payload, or ``{}`` on failure."""
    params = build_schedule_params(league_id=league_id, season=season)
    try:
        r = client.get(f"{NBA_API_ROOT}/scheduleleaguev2", params=params)
        if r.status_code != 200:
            print(f"  HTTP {r.status_code} for league={league_id} season={season}")
            return {}
        return r.json()
    except Exception as exc:  # noqa: BLE001 — best-effort scrape
        print(f"  ERROR {type(exc).__name__} for league={league_id} season={season}")
        return {}


async def main() -> None:
    """Enrich every stored competition (or a filtered set) with bracket rounds."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--league", help="Only this LeagueID (e.g. 15 for Vegas)")
    parser.add_argument("--year", type=int, help="Only this season year")
    args = parser.parse_args()

    async with SessionLocal() as db:
        stmt = select(
            SummerLeagueCompetition.year, SummerLeagueCompetition.league_id
        ).distinct()
        comps = sorted(
            (await db.execute(stmt)).all(), key=lambda r: (-r.year, r.league_id)
        )

        total_updated = 0
        with cffi.Session(
            headers=NBA_API_HEADERS, impersonate="chrome", timeout=30
        ) as client:
            for year, league_id in comps:
                if args.league and league_id != args.league:
                    continue
                if args.year and year != args.year:
                    continue
                payload = _fetch_schedule(client, league_id, year)
                time.sleep(DELAY)
                if not payload:
                    continue
                rounds = parse_schedule_rounds(payload)
                if not rounds:
                    print(f"  {year} league={league_id}: no bracket games")
                    continue
                updated = await apply_game_rounds(db, rounds)
                await db.commit()
                total_updated += updated
                print(
                    f"  {year} league={league_id}: {len(rounds)} labelled "
                    f"-> {updated} games updated"
                )

        print(f"Done. {total_updated} games tagged with a round_label.")


if __name__ == "__main__":
    asyncio.run(main())
