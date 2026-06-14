"""Integration tests for the Summer League venue and team-season pages.

- Venue (`/stats/summer-league/{year}/{venue}`): renders computed standings and
  venue leaders; 404 for an unknown venue.
- Team-season (`/stats/summer-league/{year}/{venue}/{team}`): renders record,
  roster, and schedule; 404 for an unknown team.
- Service layer: standings ordering, record/PPG computation.
"""

from __future__ import annotations

from datetime import date

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeagueGame,
    SummerLeaguePlayerGameLog,
    SummerLeagueSourcePlayer,
    SummerLeagueTeamEntry,
)
from app.services.summer_league_team_service import get_team_season, get_venue
from tests.integration.conftest import make_player

_N = {"i": 0}


async def _team(
    db: AsyncSession, *, comp_id: int, name: str, abbr: str
) -> SummerLeagueTeamEntry:
    _N["i"] += 1
    t = SummerLeagueTeamEntry(
        competition_id=comp_id,
        nba_stats_team_id=f"team-{_N['i']}",
        raw_team_name=name,
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
    home: SummerLeagueTeamEntry,
    away: SummerLeagueTeamEntry,
    home_score: int,
    away_score: int,
    game_date: date,
    log_player: PlayerMaster,
    log_team: SummerLeagueTeamEntry,
) -> SummerLeagueGame:
    _N["i"] += 1
    g = SummerLeagueGame(
        competition_id=comp_id,
        nba_stats_game_id=f"game-{_N['i']}",
        game_date=game_date,
        home_team_entry_id=home.id,
        away_team_entry_id=away.id,
        home_score=home_score,
        away_score=away_score,
    )
    db.add(g)
    await db.flush()
    assert g.id is not None
    sp = SummerLeagueSourcePlayer(
        nba_stats_person_id=f"person-{_N['i']}",
        raw_player_name=log_player.display_name or "Player",
        normalized_name=(log_player.display_name or "player").lower(),
        canonical_player_id=log_player.id,
    )
    db.add(sp)
    await db.flush()
    db.add(
        SummerLeaguePlayerGameLog(
            competition_id=comp_id,
            game_id=g.id,
            team_entry_id=log_team.id,
            source_player_id=sp.id,
            player_id=log_player.id,
            nba_stats_person_id=sp.nba_stats_person_id,
            raw_player_name=log_player.display_name or "Player",
            minutes_seconds=1800,
            pts=20,
            reb=8,
            ast=5,
            fgm=8,
            fga=15,
        )
    )
    await db.flush()
    return g


async def _seed_venue(
    db: AsyncSession, *, year: int, venue_slug: str, league_id: str, star: PlayerMaster
) -> tuple[SummerLeagueCompetition, SummerLeagueTeamEntry, SummerLeagueTeamEntry]:
    """Seed a competition with two teams; team A wins both games over team B."""
    _N["i"] += 1
    comp = SummerLeagueCompetition(
        year=year,
        league_id=league_id,
        venue_slug=venue_slug,
        display_name=f"{year} {venue_slug}",
        starts_on=date(year, 7, 1),
        ends_on=date(year, 7, 10),
    )
    db.add(comp)
    await db.flush()
    assert comp.id is not None
    team_a = await _team(db, comp_id=comp.id, name="Alpha Team", abbr="ALF")
    team_b = await _team(db, comp_id=comp.id, name="Bravo Team", abbr="BRV")

    # Game 1: A home, wins 100-90. Game 2: B home, A wins 100-80.
    await _game(
        db,
        comp_id=comp.id,
        home=team_a,
        away=team_b,
        home_score=100,
        away_score=90,
        game_date=date(year, 7, 3),
        log_player=star,
        log_team=team_a,
    )
    await _game(
        db,
        comp_id=comp.id,
        home=team_b,
        away=team_a,
        home_score=80,
        away_score=100,
        game_date=date(year, 7, 5),
        log_player=star,
        log_team=team_a,
    )
    return comp, team_a, team_b


@pytest.mark.asyncio
async def test_venue_renders_standings_and_leaders(
    app_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Venue page shows computed standings (best record first) and leaders."""
    star = make_player("Venue", "Star", school="Duke")
    db_session.add(star)
    await db_session.flush()
    _, team_a, team_b = await _seed_venue(
        db_session, year=2025, venue_slug="las_vegas", league_id="15", star=star
    )
    await db_session.commit()

    detail = await get_venue(db_session, 2025, "las_vegas")
    assert detail is not None
    assert detail.venue == "Las Vegas"
    # Team A swept (2-0) and ranks above team B (0-2).
    assert detail.standings[0].name == "Alpha Team"
    assert (detail.standings[0].wins, detail.standings[0].losses) == (2, 0)
    assert (detail.standings[1].wins, detail.standings[1].losses) == (0, 2)

    resp = await app_client.get("/stats/summer-league/2025/las_vegas")
    assert resp.status_code == 200
    html = resp.text
    assert "Las Vegas" in html
    assert "Alpha Team" in html
    assert "Venue Star" in html  # appears as a venue leader
    assert f"/stats/summer-league/2025/las_vegas/{team_a.team_slug}" in html

    missing = await app_client.get("/stats/summer-league/2025/no_such_venue")
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_team_season_renders_record_and_roster(
    app_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Team-season page shows record, quick stats, roster, schedule; 404 unknown."""
    star = make_player("Roster", "Star", school="UNC")
    db_session.add(star)
    await db_session.flush()
    _, team_a, _team_b = await _seed_venue(
        db_session, year=2025, venue_slug="las_vegas", league_id="15", star=star
    )
    await db_session.commit()

    ts = await get_team_season(db_session, 2025, "las_vegas", team_a.team_slug)
    assert ts is not None
    assert ts.name == "Alpha Team"
    assert (ts.wins, ts.losses) == (2, 0)
    assert ts.ppg == pytest.approx(100.0)
    assert ts.opp_ppg == pytest.approx(85.0)  # (90 + 80) / 2
    assert any(r.name == "Roster Star" and r.gp == 2 for r in ts.roster)

    resp = await app_client.get(
        f"/stats/summer-league/2025/las_vegas/{team_a.team_slug}"
    )
    assert resp.status_code == 200
    html = resp.text
    assert "Alpha Team" in html
    assert "Roster Star" in html
    assert "2–0" in html  # record (en dash)

    missing = await app_client.get("/stats/summer-league/2025/las_vegas/no-such-team")
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_box_score_route_wins_over_team_route(
    app_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """`/{year}/games/{id}` must resolve to the box score, not the team page.

    Pins the route precedence: the box-score route (literal ``games`` segment)
    is registered before the catch-all ``/{year}/{venue}/{team}`` route, so a
    future reorder can't silently let the team route shadow box scores.
    """
    star = make_player("Precedence", "Star", school="Duke")
    db_session.add(star)
    await db_session.flush()
    comp, team_a, _ = await _seed_venue(
        db_session, year=2025, venue_slug="las_vegas", league_id="15", star=star
    )
    await db_session.commit()

    # Grab a real game id for this competition.
    from sqlalchemy import select

    game_id = (
        (
            await db_session.execute(
                select(SummerLeagueGame.id).where(  # type: ignore[call-overload]
                    SummerLeagueGame.competition_id == comp.id  # type: ignore[arg-type]
                )
            )
        )
        .scalars()
        .first()
    )
    assert game_id is not None

    resp = await app_client.get(f"/stats/summer-league/2025/games/{game_id}")
    assert resp.status_code == 200
    # Box score-only markers (the team page has no Box Score/Advanced toggle).
    assert "Box Score" in resp.text and "Advanced" in resp.text
