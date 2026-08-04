"""Integration tests for the game box-score shot-chart wiring.

Exercises GET /stats/summer-league/{year}/games/{game_id} with and without
shot-event data, verifying:

  1. Game with shot events → shot chart section rendered and every scope
     preloaded into window.SL_SHOTCHART_SCOPES (client-side switching).
  2. get_game_shotchart_scopes groups shots into game / team / player scopes.
  3. get_game_shotchart_context still scopes a single payload (retained helper).
  4. Game without shot events → graceful "no shot data" empty state.
"""

from __future__ import annotations

from datetime import date

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import (
    SummerLeagueEdition,
    SummerLeagueGame,
    SummerLeaguePlayerGameLog,
    SummerLeagueShotEvent,
    SummerLeagueSourcePlayer,
    SummerLeagueTeamEntry,
)
from app.services.summer_league_games_service import get_game_shotchart_context
from app.services.summer_league_shotchart_service import get_game_shotchart_scopes
from tests.integration.conftest import make_player

_SEQ: dict[str, int] = {"n": 0}


def _uid() -> str:
    _SEQ["n"] += 1
    return f"gsc-{_SEQ['n']}"


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


async def _make_comp(db: AsyncSession, *, year: int = 2025) -> SummerLeagueEdition:
    comp = SummerLeagueEdition(
        year=year,
        league_id=_uid(),
        venue_slug="las_vegas",
        display_name=f"{year} Las Vegas Summer League",
    )
    db.add(comp)
    await db.flush()
    return comp


async def _make_team(
    db: AsyncSession, *, comp_id: int, name: str = "Team"
) -> SummerLeagueTeamEntry:
    team = SummerLeagueTeamEntry(
        competition_id=comp_id,
        nba_stats_team_id=_uid(),
        raw_team_name=name,
        raw_team_abbreviation=name[:3].upper(),
        team_slug=f"{name.lower().replace(' ', '-')}-{_uid()}",
    )
    db.add(team)
    await db.flush()
    return team


async def _make_source_player(
    db: AsyncSession, *, player: PlayerMaster
) -> SummerLeagueSourcePlayer:
    sp = SummerLeagueSourcePlayer(
        nba_stats_person_id=_uid(),
        raw_player_name=player.display_name or "Player",
        normalized_name=(player.display_name or "player").lower(),
        canonical_player_id=player.id,
    )
    db.add(sp)
    await db.flush()
    return sp


async def _make_game(
    db: AsyncSession,
    *,
    comp: SummerLeagueEdition,
    home: SummerLeagueTeamEntry,
    away: SummerLeagueTeamEntry,
    game_date: date = date(2025, 7, 10),
) -> SummerLeagueGame:
    game = SummerLeagueGame(
        competition_id=comp.id,
        nba_stats_game_id=_uid(),
        game_date=game_date,
        home_team_entry_id=home.id,
        away_team_entry_id=away.id,
        home_score=110,
        away_score=95,
    )
    db.add(game)
    await db.flush()
    return game


async def _make_log(
    db: AsyncSession,
    *,
    comp: SummerLeagueEdition,
    game: SummerLeagueGame,
    team: SummerLeagueTeamEntry,
    player: PlayerMaster,
    sp: SummerLeagueSourcePlayer,
    pts: int = 15,
    fga: int = 10,
    fgm: int = 6,
) -> None:
    db.add(
        SummerLeaguePlayerGameLog(
            competition_id=comp.id,
            game_id=game.id,
            team_entry_id=team.id,
            source_player_id=sp.id,
            player_id=player.id,
            nba_stats_person_id=sp.nba_stats_person_id,
            raw_player_name=player.display_name or "Player",
            minutes_seconds=1800,
            pts=pts,
            reb=5,
            ast=3,
            fgm=fgm,
            fga=fga,
        )
    )
    await db.flush()


def _make_shot_event(
    *,
    game: SummerLeagueGame,
    comp: SummerLeagueEdition,
    team: SummerLeagueTeamEntry,
    player: PlayerMaster,
    sp: SummerLeagueSourcePlayer,
    zone: str,
    made: bool,
    event_num: int,
) -> SummerLeagueShotEvent:
    return SummerLeagueShotEvent(
        game_id=game.id,
        competition_id=comp.id,
        team_entry_id=team.id,
        source_player_id=sp.id,
        player_id=player.id,
        nba_stats_person_id=sp.nba_stats_person_id,
        nba_stats_game_id=game.nba_stats_game_id,
        nba_stats_game_event_id=event_num,
        shot_zone_basic=zone,
        loc_x=100 + event_num,
        loc_y=200 + event_num,
        made=made,
    )


async def _seed_game_with_shots(
    db: AsyncSession,
) -> tuple[
    PlayerMaster,
    PlayerMaster,
    SummerLeagueGame,
    SummerLeagueTeamEntry,
    SummerLeagueTeamEntry,
]:
    """Seed two players on opposing teams with 25 shot events each (above MIN_FGA_FOR_CHART=20)."""
    comp = await _make_comp(db)
    assert comp.id is not None

    home = await _make_team(db, comp_id=comp.id, name="Home Squad")
    away = await _make_team(db, comp_id=comp.id, name="Away Squad")
    assert home.id is not None and away.id is not None

    home_player = make_player("Home", "Shooter", school="Duke")
    away_player = make_player("Away", "Gunner", school="UNC")
    db.add(home_player)
    db.add(away_player)
    await db.flush()
    await db.refresh(home_player)
    await db.refresh(away_player)
    assert home_player.id is not None and away_player.id is not None

    home_sp = await _make_source_player(db, player=home_player)
    away_sp = await _make_source_player(db, player=away_player)

    game = await _make_game(db, comp=comp, home=home, away=away)
    assert game.id is not None

    await _make_log(db, comp=comp, game=game, team=home, player=home_player, sp=home_sp)
    await _make_log(db, comp=comp, game=game, team=away, player=away_player, sp=away_sp)

    # 25 home player shots
    for i in range(25):
        db.add(
            _make_shot_event(
                game=game,
                comp=comp,
                team=home,
                player=home_player,
                sp=home_sp,
                zone="Restricted Area",
                made=(i % 2 == 0),
                event_num=i + 1,
            )
        )

    # 25 away player shots in a different zone
    for i in range(25):
        db.add(
            _make_shot_event(
                game=game,
                comp=comp,
                team=away,
                player=away_player,
                sp=away_sp,
                zone="Above the Break 3",
                made=(i % 3 == 0),
                event_num=100 + i,
            )
        )

    await db.flush()
    return home_player, away_player, game, home, away


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_game_box_shotchart_whole_game_scope(
    app_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Game with 50 shot events: whole-game chart visible in default render."""
    home_player, away_player, game, home, away = await _seed_game_with_shots(db_session)
    await db_session.commit()

    assert game.id is not None
    resp = await app_client.get(f"/stats/summer-league/2025/games/{game.id}")
    assert resp.status_code == 200
    html = resp.text

    # Shot chart section rendered.
    assert "sl-shotchart-section" in html
    assert "Shot Chart" in html
    assert 'id="sl-shotchart-root"' in html

    # Every scope preloaded for client-side switching.
    assert "window.SL_SHOTCHART_SCOPES" in html
    # Zone labels ship inside the preloaded scope payload.
    assert "Restricted Area" in html
    assert "Above the Break 3" in html

    # Both the shared renderer and the game controller load.
    assert "summer-league-shotchart.js" in html
    assert "summer-league-game-shotchart.js" in html

    # Selector shows team buttons + per-team player sub-selectors.
    assert "Whole Game" in html
    assert "HOM" in html or "Home Squad" in html
    assert "AWA" in html or "Away Squad" in html
    assert "slg-shotchart-player-selector" in html
    assert "Home Shooter" in html and "Away Gunner" in html


@pytest.mark.asyncio
async def test_game_shotchart_scopes_service(
    db_session: AsyncSession,
) -> None:
    """get_game_shotchart_scopes groups a game's shots into game/team/player."""
    home_player, away_player, game, home, away = await _seed_game_with_shots(db_session)
    await db_session.commit()

    assert game.id is not None
    result = await get_game_shotchart_scopes(db_session, game_id=game.id)
    assert result is not None
    scopes = result["scopes"]

    # Whole game holds all 50 shots across both zones.
    assert scopes["game"]["total_fga"] == 50
    game_zones = {z["shot_zone_basic"] for z in scopes["game"]["zones"]}
    assert {"Restricted Area", "Above the Break 3"} <= game_zones

    # Team scope is only that team's 25 shots, one zone.
    home_scope = scopes[f"team:{home.id}"]
    assert home_scope["total_fga"] == 25
    assert {z["shot_zone_basic"] for z in home_scope["zones"]} == {"Restricted Area"}
    assert len(home_scope["dots"]) == 25

    # Player scope resolves to that player's shots.
    player_scope = scopes[f"player:{away_player.id}"]
    assert player_scope["total_fga"] == 25
    assert {z["shot_zone_basic"] for z in player_scope["zones"]} == {
        "Above the Break 3"
    }


@pytest.mark.asyncio
async def test_game_box_shotchart_service_team_scope(
    db_session: AsyncSession,
) -> None:
    """Service returns None for team that has no shots in the game."""
    home_player, away_player, game, home, away = await _seed_game_with_shots(db_session)
    await db_session.commit()

    assert game.id is not None and home.id is not None and away.id is not None

    # Whole-game context: non-null, both zones present.
    ctx = await get_game_shotchart_context(db_session, game.id)
    assert ctx is not None
    assert ctx["total_fga"] == 50
    zone_names = {z["shot_zone_basic"] for z in ctx["zones"]}
    assert "Restricted Area" in zone_names
    assert "Above the Break 3" in zone_names

    # Home-scoped: only Restricted Area.
    home_ctx = await get_game_shotchart_context(
        db_session, game.id, team_entry_id=home.id
    )
    assert home_ctx is not None
    assert home_ctx["total_fga"] == 25
    home_zones = {z["shot_zone_basic"] for z in home_ctx["zones"]}
    assert "Restricted Area" in home_zones
    assert "Above the Break 3" not in home_zones

    # Dots included when coordinates present.
    assert len(home_ctx["dots"]) == 25


@pytest.mark.asyncio
async def test_game_box_shotchart_empty_state_no_shot_data(
    app_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Game without shot events renders graceful empty state, not an error."""
    comp = await _make_comp(db_session)
    assert comp.id is not None

    home = await _make_team(db_session, comp_id=comp.id, name="No Shots Home")
    away = await _make_team(db_session, comp_id=comp.id, name="No Shots Away")
    player = make_player("No", "Shots", school="Kansas")
    db_session.add(player)
    await db_session.flush()
    await db_session.refresh(player)
    assert player.id is not None

    sp = await _make_source_player(db_session, player=player)
    game = await _make_game(db_session, comp=comp, home=home, away=away)
    await _make_log(db_session, comp=comp, game=game, team=home, player=player, sp=sp)
    await db_session.commit()

    assert game.id is not None
    resp = await app_client.get(f"/stats/summer-league/2025/games/{game.id}")
    assert resp.status_code == 200
    html = resp.text

    # Shot chart section present but no chart injected.
    assert "sl-shotchart-section" in html
    assert "window.SL_SHOTCHART_SCOPES" not in html
    assert "No shot-chart data available" in html
    assert "summer-league-shotchart.js" not in html
