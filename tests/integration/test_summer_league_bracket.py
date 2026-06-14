"""Integration tests for the Summer League championship bracket.

- ``apply_game_rounds`` tags games by NBA Stats game id.
- ``get_venue_bracket`` structures the tagged games into semifinals + final.
- The venue page renders a "Championship Bracket" only when bracket games exist.
"""

from __future__ import annotations

from datetime import date

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeagueGame,
    SummerLeagueTeamEntry,
)
from app.services.summer_league.bracket import apply_game_rounds
from app.services.summer_league_team_service import get_venue_bracket

_N = {"i": 0}


async def _team(db: AsyncSession, comp_id: int, abbr: str) -> SummerLeagueTeamEntry:
    _N["i"] += 1
    t = SummerLeagueTeamEntry(
        competition_id=comp_id,
        nba_stats_team_id=f"t-{_N['i']}",
        raw_team_name=f"{abbr} Team",
        raw_team_abbreviation=abbr,
        team_slug=f"{abbr.lower()}-{_N['i']}",
    )
    db.add(t)
    await db.flush()
    return t


async def _game(
    db: AsyncSession,
    *,
    comp_id: int,
    nba_id: str,
    home: SummerLeagueTeamEntry,
    away: SummerLeagueTeamEntry,
    home_score: int,
    away_score: int,
    when: date,
) -> SummerLeagueGame:
    g = SummerLeagueGame(
        competition_id=comp_id,
        nba_stats_game_id=nba_id,
        game_date=when,
        home_team_entry_id=home.id,
        away_team_entry_id=away.id,
        home_score=home_score,
        away_score=away_score,
    )
    db.add(g)
    await db.flush()
    return g


async def _seed_vegas_bracket(db: AsyncSession, *, year: int = 2025) -> None:
    """Seed a Vegas competition with two semifinals and a final (unlabelled)."""
    comp = SummerLeagueCompetition(
        year=year,
        league_id="15",
        venue_slug="las_vegas",
        display_name=f"{year} Las Vegas",
        starts_on=date(year, 7, 1),
        ends_on=date(year, 7, 17),
    )
    db.add(comp)
    await db.flush()
    assert comp.id is not None
    a = await _team(db, comp.id, "AAA")
    b = await _team(db, comp.id, "BBB")
    c = await _team(db, comp.id, "CCC")
    d = await _team(db, comp.id, "DDD")
    # Semifinals: A beats B, C beats D. Final: C beats A (C is away).
    await _game(
        db,
        comp_id=comp.id,
        nba_id="g-semi1",
        home=a,
        away=b,
        home_score=90,
        away_score=80,
        when=date(year, 7, 15),
    )
    await _game(
        db,
        comp_id=comp.id,
        nba_id="g-semi2",
        home=c,
        away=d,
        home_score=88,
        away_score=70,
        when=date(year, 7, 15),
    )
    await _game(
        db,
        comp_id=comp.id,
        nba_id="g-final",
        home=a,
        away=c,
        home_score=78,
        away_score=85,
        when=date(year, 7, 17),
    )


@pytest.mark.asyncio
async def test_apply_rounds_and_get_bracket(
    app_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Applying schedule rounds builds a semifinals + final bracket."""
    await _seed_vegas_bracket(db_session)
    await db_session.commit()

    updated = await apply_game_rounds(
        db_session,
        {
            "g-semi1": "Semifinals",
            "g-semi2": "Semifinals",
            "g-final": "Championship",
            "g-does-not-exist": "Championship",  # no-op
        },
    )
    await db_session.commit()
    assert updated == 3  # the bogus id matched nothing

    bracket = await get_venue_bracket(db_session, 2025, "las_vegas")
    assert bracket is not None
    assert len(bracket.semifinals) == 2
    assert bracket.final is not None
    # Final: home AAA 78, away CCC 85 -> away wins.
    assert bracket.final.winner == "away"
    assert {g.winner for g in bracket.semifinals} == {"home"}  # both home teams won

    resp = await app_client.get("/stats/summer-league/2025/las_vegas")
    assert resp.status_code == 200
    assert "Championship Bracket" in resp.text
    assert "CCC" in resp.text and "AAA" in resp.text


@pytest.mark.asyncio
async def test_no_bracket_when_unlabelled(
    app_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """A competition with no round labels has no bracket section."""
    await _seed_vegas_bracket(db_session, year=2023)
    await db_session.commit()

    bracket = await get_venue_bracket(db_session, 2023, "las_vegas")
    assert bracket is None

    resp = await app_client.get("/stats/summer-league/2023/las_vegas")
    assert resp.status_code == 200
    assert "Championship Bracket" not in resp.text
