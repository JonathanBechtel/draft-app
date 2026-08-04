"""Integration test: venue standings surface NBA franchise logos.

Franchise teams (canonical stats id) get a logo; exhibition squads do not.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.summer_league import (
    SummerLeagueEdition,
    SummerLeagueGame,
    SummerLeagueTeamEntry,
)
from app.services.summer_league_team_service import get_venue


@pytest.mark.asyncio
async def test_standings_show_franchise_logo(db_session: AsyncSession) -> None:
    """A franchise team's standings row carries its NBA CDN logo; exhibition none."""
    comp = SummerLeagueEdition(
        year=2025,
        league_id="15",
        venue_slug="las_vegas",
        display_name="2025 Las Vegas",
        starts_on=date(2025, 7, 1),
        ends_on=date(2025, 7, 17),
    )
    db_session.add(comp)
    await db_session.flush()
    assert comp.id is not None

    # Charlotte (franchise stats id 1610612766) vs an exhibition squad.
    cha = SummerLeagueTeamEntry(
        competition_id=comp.id,
        nba_stats_team_id="1610612766",
        raw_team_name="Charlotte Hornets",
        raw_team_abbreviation="CHA",
        team_slug="cha",
    )
    orw = SummerLeagueTeamEntry(
        competition_id=comp.id,
        nba_stats_team_id="1612709900",  # not a franchise id
        raw_team_name="Orlando White",
        raw_team_abbreviation="ORW",
        team_slug="orw",
    )
    db_session.add_all([cha, orw])
    await db_session.flush()
    db_session.add(
        SummerLeagueGame(
            competition_id=comp.id,
            nba_stats_game_id="g-logo-1",
            game_date=date(2025, 7, 5),
            home_team_entry_id=cha.id,
            away_team_entry_id=orw.id,
            home_score=100,
            away_score=90,
        )
    )
    await db_session.commit()

    detail = await get_venue(db_session, 2025, "las_vegas")
    assert detail is not None
    by_name = {s.name: s for s in detail.standings}
    assert by_name["Charlotte Hornets"].logo_url == "/static/logos/nba/1610612766.svg"
    assert by_name["Orlando White"].logo_url is None
