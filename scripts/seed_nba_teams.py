#!/usr/bin/env python
"""Seed the nba_teams table with all 30 current NBA franchises.

Usage:
    python scripts/seed_nba_teams.py

Idempotent: skips teams that already exist (matched by abbreviation).
"""

import asyncio
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

load_dotenv()

# fmt: off
NBA_TEAMS = [
    # Atlantic Division — Eastern Conference
    {"name": "Boston Celtics",       "abbreviation": "BOS", "slug": "celtics",       "city": "Boston",        "conference": "Eastern", "division": "Atlantic",  "primary_color": "#007A33", "secondary_color": "#BA9653"},
    {"name": "Brooklyn Nets",        "abbreviation": "BKN", "slug": "nets",          "city": "Brooklyn",      "conference": "Eastern", "division": "Atlantic",  "primary_color": "#000000", "secondary_color": "#FFFFFF"},
    {"name": "New York Knicks",      "abbreviation": "NYK", "slug": "knicks",        "city": "New York",      "conference": "Eastern", "division": "Atlantic",  "primary_color": "#006BB6", "secondary_color": "#F58426"},
    {"name": "Philadelphia 76ers",   "abbreviation": "PHI", "slug": "76ers",         "city": "Philadelphia",  "conference": "Eastern", "division": "Atlantic",  "primary_color": "#006BB6", "secondary_color": "#ED174C"},
    {"name": "Toronto Raptors",      "abbreviation": "TOR", "slug": "raptors",       "city": "Toronto",       "conference": "Eastern", "division": "Atlantic",  "primary_color": "#CE1141", "secondary_color": "#000000"},
    # Central Division — Eastern Conference
    {"name": "Chicago Bulls",        "abbreviation": "CHI", "slug": "bulls",         "city": "Chicago",       "conference": "Eastern", "division": "Central",   "primary_color": "#CE1141", "secondary_color": "#000000"},
    {"name": "Cleveland Cavaliers",  "abbreviation": "CLE", "slug": "cavaliers",     "city": "Cleveland",     "conference": "Eastern", "division": "Central",   "primary_color": "#860038", "secondary_color": "#FDBB30"},
    {"name": "Detroit Pistons",      "abbreviation": "DET", "slug": "pistons",       "city": "Detroit",       "conference": "Eastern", "division": "Central",   "primary_color": "#C8102E", "secondary_color": "#1D42BA"},
    {"name": "Indiana Pacers",       "abbreviation": "IND", "slug": "pacers",        "city": "Indianapolis",  "conference": "Eastern", "division": "Central",   "primary_color": "#002D62", "secondary_color": "#FDBB30"},
    {"name": "Milwaukee Bucks",      "abbreviation": "MIL", "slug": "bucks",         "city": "Milwaukee",     "conference": "Eastern", "division": "Central",   "primary_color": "#00471B", "secondary_color": "#EEE1C6"},
    # Southeast Division — Eastern Conference
    {"name": "Atlanta Hawks",        "abbreviation": "ATL", "slug": "hawks",         "city": "Atlanta",       "conference": "Eastern", "division": "Southeast", "primary_color": "#E03A3E", "secondary_color": "#C1D32F"},
    {"name": "Charlotte Hornets",    "abbreviation": "CHA", "slug": "hornets",       "city": "Charlotte",     "conference": "Eastern", "division": "Southeast", "primary_color": "#1D1160", "secondary_color": "#00788C"},
    {"name": "Miami Heat",           "abbreviation": "MIA", "slug": "heat",          "city": "Miami",         "conference": "Eastern", "division": "Southeast", "primary_color": "#98002E", "secondary_color": "#F9A01B"},
    {"name": "Orlando Magic",        "abbreviation": "ORL", "slug": "magic",         "city": "Orlando",       "conference": "Eastern", "division": "Southeast", "primary_color": "#0077C0", "secondary_color": "#C4CED4"},
    {"name": "Washington Wizards",   "abbreviation": "WAS", "slug": "wizards",       "city": "Washington",    "conference": "Eastern", "division": "Southeast", "primary_color": "#002B5C", "secondary_color": "#E31837"},
    # Northwest Division — Western Conference
    {"name": "Denver Nuggets",       "abbreviation": "DEN", "slug": "nuggets",       "city": "Denver",        "conference": "Western", "division": "Northwest", "primary_color": "#0E2240", "secondary_color": "#FEC524"},
    {"name": "Minnesota Timberwolves", "abbreviation": "MIN", "slug": "timberwolves", "city": "Minneapolis", "conference": "Western", "division": "Northwest", "primary_color": "#0C2340", "secondary_color": "#236192"},
    {"name": "Oklahoma City Thunder", "abbreviation": "OKC", "slug": "thunder",      "city": "Oklahoma City", "conference": "Western", "division": "Northwest", "primary_color": "#007AC1", "secondary_color": "#EF6020"},
    {"name": "Portland Trail Blazers", "abbreviation": "POR", "slug": "trail-blazers", "city": "Portland",   "conference": "Western", "division": "Northwest", "primary_color": "#E03A3E", "secondary_color": "#000000"},
    {"name": "Utah Jazz",            "abbreviation": "UTA", "slug": "jazz",          "city": "Salt Lake City", "conference": "Western", "division": "Northwest", "primary_color": "#002B5C", "secondary_color": "#F9A01B"},
    # Pacific Division — Western Conference
    {"name": "Golden State Warriors", "abbreviation": "GSW", "slug": "warriors",     "city": "San Francisco", "conference": "Western", "division": "Pacific",   "primary_color": "#1D428A", "secondary_color": "#FFC72C"},
    {"name": "Los Angeles Clippers", "abbreviation": "LAC", "slug": "clippers",      "city": "Los Angeles",   "conference": "Western", "division": "Pacific",   "primary_color": "#C8102E", "secondary_color": "#1D428A"},
    {"name": "Los Angeles Lakers",   "abbreviation": "LAL", "slug": "lakers",        "city": "Los Angeles",   "conference": "Western", "division": "Pacific",   "primary_color": "#552583", "secondary_color": "#FDB927"},
    {"name": "Phoenix Suns",         "abbreviation": "PHX", "slug": "suns",          "city": "Phoenix",       "conference": "Western", "division": "Pacific",   "primary_color": "#1D1160", "secondary_color": "#E56020"},
    {"name": "Sacramento Kings",     "abbreviation": "SAC", "slug": "kings",         "city": "Sacramento",    "conference": "Western", "division": "Pacific",   "primary_color": "#5A2D81", "secondary_color": "#63727A"},
    # Southwest Division — Western Conference
    {"name": "Dallas Mavericks",     "abbreviation": "DAL", "slug": "mavericks",     "city": "Dallas",        "conference": "Western", "division": "Southwest", "primary_color": "#00538C", "secondary_color": "#002B5E"},
    {"name": "Houston Rockets",      "abbreviation": "HOU", "slug": "rockets",       "city": "Houston",       "conference": "Western", "division": "Southwest", "primary_color": "#CE1141", "secondary_color": "#000000"},
    {"name": "Memphis Grizzlies",    "abbreviation": "MEM", "slug": "grizzlies",     "city": "Memphis",       "conference": "Western", "division": "Southwest", "primary_color": "#5D76A9", "secondary_color": "#12173F"},
    {"name": "New Orleans Pelicans",  "abbreviation": "NOP", "slug": "pelicans",     "city": "New Orleans",   "conference": "Western", "division": "Southwest", "primary_color": "#0C2340", "secondary_color": "#C8102E"},
    {"name": "San Antonio Spurs",    "abbreviation": "SAS", "slug": "spurs",         "city": "San Antonio",   "conference": "Western", "division": "Southwest", "primary_color": "#C4CED4", "secondary_color": "#000000"},
]
# fmt: on


async def seed_nba_teams() -> None:
    """Insert NBA teams into nba_teams table, skipping existing rows."""
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set")
        sys.exit(1)

    engine = create_async_engine(db_url)
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    # Import here to avoid import-time side effects
    from app.schemas.nba_teams import NbaTeam

    added = 0
    skipped = 0

    async with session_factory() as session:
        for team_data in NBA_TEAMS:
            result = await session.execute(
                select(NbaTeam).where(NbaTeam.abbreviation == team_data["abbreviation"])
            )
            existing = result.scalar_one_or_none()

            if existing:
                print(f"  SKIP  {team_data['abbreviation']} — {team_data['name']}")
                skipped += 1
                continue

            now = datetime.now(timezone.utc).replace(tzinfo=None)
            team = NbaTeam(
                **team_data,
                created_at=now,
                updated_at=now,
            )
            session.add(team)
            print(f"  ADD   {team_data['abbreviation']} — {team_data['name']}")
            added += 1

        await session.commit()

    await engine.dispose()
    print(f"\nDone: {added} added, {skipped} skipped ({added + skipped} total)")


if __name__ == "__main__":
    asyncio.run(seed_nba_teams())
