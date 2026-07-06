"""Integration tests for the Summer League landing and season-hub pages.

- Landing (`/stats/summer-league`): renders the year strip, latest-season hero,
  leaders, and recent games.
- Season hub (`/stats/summer-league/{year}`): renders venues, leaders, and the
  schedule; 404s for a year with no competitions.
- Service layer: season overview counts and per-game leader ordering.
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
from app.services.summer_league_season_service import (
    get_alltime_leaders,
    get_season_leaders,
    get_season_overview,
    get_season_years,
)
from tests.integration.conftest import make_player

_N = {"i": 0}


async def _seed_competition(
    db: AsyncSession,
    *,
    player: PlayerMaster,
    year: int,
    venue_slug: str,
    league_id: str,
    n_games: int = 2,
    pts: int = 20,
    reb: int = 8,
    ast: int = 5,
) -> SummerLeagueCompetition:
    """Seed one competition with ``n_games`` games and a per-game log for player."""
    _N["i"] += 1
    comp = SummerLeagueCompetition(
        year=year,
        league_id=league_id,
        venue_slug=venue_slug,
        display_name=f"{year} {venue_slug}",
        starts_on=date(year, 7, 1),
        ends_on=date(year, 7, 15),
    )
    db.add(comp)
    await db.flush()
    assert comp.id is not None

    home = SummerLeagueTeamEntry(
        competition_id=comp.id,
        nba_stats_team_id=f"h-{_N['i']}",
        raw_team_name="Home Team",
        raw_team_abbreviation="HOM",
        team_slug=f"hom-{_N['i']}",
    )
    away = SummerLeagueTeamEntry(
        competition_id=comp.id,
        nba_stats_team_id=f"a-{_N['i']}",
        raw_team_name="Away Team",
        raw_team_abbreviation="AWY",
        team_slug=f"awy-{_N['i']}",
    )
    db.add_all([home, away])
    await db.flush()
    assert home.id is not None and away.id is not None

    sp = SummerLeagueSourcePlayer(
        nba_stats_person_id=f"p-{_N['i']}",
        raw_player_name=player.display_name or "Player",
        normalized_name=(player.display_name or "player").lower(),
        canonical_player_id=player.id,
    )
    db.add(sp)
    await db.flush()

    for g in range(n_games):
        _N["i"] += 1
        game = SummerLeagueGame(
            competition_id=comp.id,
            nba_stats_game_id=f"g-{_N['i']}",
            game_date=date(year, 7, 5 + g),
            home_team_entry_id=home.id,
            away_team_entry_id=away.id,
            home_score=100,
            away_score=90,
        )
        db.add(game)
        await db.flush()
        assert game.id is not None
        db.add(
            SummerLeaguePlayerGameLog(
                competition_id=comp.id,
                game_id=game.id,
                team_entry_id=home.id,
                source_player_id=sp.id,
                player_id=player.id,
                nba_stats_person_id=sp.nba_stats_person_id,
                raw_player_name=player.display_name or "Player",
                minutes_seconds=1800,
                pts=pts,
                reb=reb,
                ast=ast,
            )
        )
    await db.flush()
    return comp


@pytest.mark.asyncio
async def test_season_landing_renders(
    app_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Landing shows the year strip, latest-season hero, leaders, recent games."""
    star = make_player("Landing", "Star", school="Duke")
    db_session.add(star)
    await db_session.flush()
    # 3 + 3 = 6 career games so the star clears the all-time min-games floor.
    await _seed_competition(
        db_session,
        player=star,
        year=2025,
        venue_slug="las_vegas",
        league_id="15",
        n_games=3,
    )
    await _seed_competition(
        db_session,
        player=star,
        year=2024,
        venue_slug="las_vegas",
        league_id="15",
        n_games=3,
    )
    await db_session.commit()

    years = await get_season_years(db_session)
    assert years[:2] == [2025, 2024]

    # All-time leaders use career totals: 6 games x 20 pts = 120.
    alltime = await get_alltime_leaders(db_session)
    assert alltime.pts[0].name == "Landing Star"
    assert alltime.pts[0].value == pytest.approx(120.0)

    resp = await app_client.get("/stats/summer-league")
    assert resp.status_code == 200
    html = resp.text
    assert "NBA Summer League" in html
    assert "/stats/summer-league/2025" in html  # year strip + hero link
    assert "All-Time Leaders" in html
    assert "Landing Star" in html  # hero top scorer + all-time leader
    assert "/stats/summer-league/games" in html  # game-finder CTA


@pytest.mark.asyncio
async def test_season_hub_renders_with_venues_and_leaders(
    app_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Season hub renders venue cards, leaders, schedule; service counts match."""
    star = make_player("Hub", "Star", school="UNC")
    bench = make_player("One", "Gamer", school="Iowa")
    db_session.add_all([star, bench])
    await db_session.flush()

    # Two venues this year; star plays 2 games in Vegas, bench only 1 (below min).
    await _seed_competition(
        db_session, player=star, year=2025, venue_slug="las_vegas", league_id="15"
    )
    await _seed_competition(
        db_session,
        player=bench,
        year=2025,
        venue_slug="salt_lake_city",
        league_id="16",
        n_games=1,
    )
    await db_session.commit()

    overview = await get_season_overview(db_session, 2025)
    assert overview is not None
    assert {v.venue for v in overview.venues} == {"Las Vegas", "Salt Lake City"}
    assert overview.total_games == 3  # 2 + 1

    leaders = await get_season_leaders(db_session, 2025, min_games=2)
    # A single standard qualifier is short of a full top-5 board, so the
    # fallback backfills with the 1-game player (both average 20.0 PPG).
    assert {r.name for r in leaders.pts} == {"Hub Star", "One Gamer"}
    assert leaders.pts[0].value == pytest.approx(20.0)

    resp = await app_client.get("/stats/summer-league/2025")
    assert resp.status_code == 200
    html = resp.text
    assert "2025 NBA Summer League" in html
    assert "Las Vegas" in html and "Salt Lake City" in html
    assert "Hub Star" in html
    assert "/stats/summer-league/games?year=2025" in html


@pytest.mark.asyncio
async def test_season_hub_unknown_year_404(
    app_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """A year with no competitions 404s."""
    resp = await app_client.get("/stats/summer-league/1999")
    assert resp.status_code == 404
