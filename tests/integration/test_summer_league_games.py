"""Integration tests for the Summer League games store.

Covers the three public surfaces and their service layer:

- Games index (`/stats/summer-league/games`): lists games, filters by year /
  venue / player, paginates.
- Game box score (`/stats/summer-league/{year}/games/{game_id}`): renders both
  teams' lines; 404 for a missing game.
- Player game logs (`/players/{slug}/summer-league`): groups by competition;
  404 for an unknown player.
- The player-detail hook: a "Game Log" button + box-score links.
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
    SummerLeagueSourcePlayer,
    SummerLeagueTeamEntry,
    SummerLeagueTeamGameLog,
)
from app.services.summer_league_games_service import (
    get_game_box_score,
    get_games_facets,
    search_games,
)
from tests.integration.conftest import make_player

_SEQ = {"n": 0}


async def _team(
    db: AsyncSession, *, comp_id: int, name: str, abbr: str
) -> SummerLeagueTeamEntry:
    _SEQ["n"] += 1
    team = SummerLeagueTeamEntry(
        competition_id=comp_id,
        nba_stats_team_id=f"team-{_SEQ['n']}",
        raw_team_name=name,
        raw_team_abbreviation=abbr,
        team_slug=f"{abbr.lower()}-{_SEQ['n']}",
    )
    db.add(team)
    await db.flush()
    return team


async def _source_player(
    db: AsyncSession, *, player: PlayerMaster
) -> SummerLeagueSourcePlayer:
    _SEQ["n"] += 1
    sp = SummerLeagueSourcePlayer(
        nba_stats_person_id=f"person-{_SEQ['n']}",
        raw_player_name=player.display_name or "Player",
        normalized_name=(player.display_name or "player").lower(),
        canonical_player_id=player.id,
    )
    db.add(sp)
    await db.flush()
    return sp


async def _seed_game(
    db: AsyncSession,
    *,
    home_player: PlayerMaster,
    year: int,
    league_id: str,
    venue_slug: str,
    game_date: date,
    home_pts: int = 100,
    away_pts: int = 90,
) -> SummerLeagueGame:
    """Seed one competition/game with a home + away team and one home log."""
    _SEQ["n"] += 1
    comp = SummerLeagueEdition(
        year=year,
        league_id=league_id,
        venue_slug=venue_slug,
        display_name=f"{year} {venue_slug}",
    )
    db.add(comp)
    await db.flush()
    assert comp.id is not None

    home = await _team(db, comp_id=comp.id, name="Home Team", abbr="HOM")
    away = await _team(db, comp_id=comp.id, name="Away Team", abbr="AWY")
    assert home.id is not None and away.id is not None

    game = SummerLeagueGame(
        competition_id=comp.id,
        nba_stats_game_id=f"game-{_SEQ['n']}",
        game_date=game_date,
        home_team_entry_id=home.id,
        away_team_entry_id=away.id,
        home_score=home_pts,
        away_score=away_pts,
    )
    db.add(game)
    await db.flush()
    assert game.id is not None

    sp = await _source_player(db, player=home_player)
    db.add(
        SummerLeaguePlayerGameLog(
            competition_id=comp.id,
            game_id=game.id,
            team_entry_id=home.id,
            source_player_id=sp.id,
            player_id=home_player.id,
            nba_stats_person_id=sp.nba_stats_person_id,
            raw_player_name=home_player.display_name or "Player",
            minutes_seconds=1800,
            pts=20,
            oreb=3,
            dreb=5,
            reb=8,
            ast=5,
            stl=1,
            blk=1,
            tov=2,
            pf=2,
            fgm=8,
            fga=15,
            fg3m=2,
            fg3a=5,
            ftm=2,
            fta=2,
            plus_minus=10,
            ts_pct=0.6,
            efg_pct=0.57,
            usg_pct=0.25,
        )
    )
    await db.flush()
    return game


@pytest.mark.asyncio
async def test_games_index_lists_filters_and_paginates(
    app_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Index renders seeded games and respects year / venue / player filters."""
    player = make_player("Index", "Tester", school="Duke")
    db_session.add(player)
    await db_session.flush()

    g2025 = await _seed_game(
        db_session,
        home_player=player,
        year=2025,
        league_id="15",
        venue_slug="las_vegas",
        game_date=date(2025, 7, 12),
    )
    await _seed_game(
        db_session,
        home_player=player,
        year=2024,
        league_id="13",
        venue_slug="california_classic",
        game_date=date(2024, 7, 6),
    )
    await db_session.commit()

    # Unfiltered: both seeded games present.
    assert player.id is not None
    all_page = await search_games(db_session)
    assert all_page.total >= 2
    assert all_page.page == 1

    # Year filter.
    only_2025 = await search_games(db_session, year=2025)
    assert all(g.year == 2025 for g in only_2025.games)
    assert g2025.id in {g.game_id for g in only_2025.games}

    # Venue filter.
    cc = await search_games(db_session, venue_slug="california_classic")
    assert all(g.venue_slug == "california_classic" for g in cc.games)

    # Player filter.
    by_player = await search_games(db_session, player_id=player.id)
    assert by_player.total == 2

    # Pagination metadata is coherent.
    paged = await search_games(db_session, page=1, page_size=1)
    assert paged.page_size == 1
    assert paged.total_pages >= 2
    assert len(paged.games) == 1

    # Facets surface the seeded years/venues.
    facets = await get_games_facets(db_session)
    assert 2025 in facets.years and 2024 in facets.years
    venue_values = {v.value for v in facets.venues}
    assert {"las_vegas", "california_classic"} <= venue_values

    # The HTTP surface renders and links to the box score.
    resp = await app_client.get("/stats/summer-league/games")
    assert resp.status_code == 200
    assert "Game Finder" in resp.text
    assert f"/stats/summer-league/2025/games/{g2025.id}" in resp.text

    # Player-filtered HTTP view names the player.
    resp_p = await app_client.get(f"/stats/summer-league/games?player={player.slug}")
    assert resp_p.status_code == 200
    assert "Index Tester" in resp_p.text

    # An unresolvable player filter renders no games (not silently all games).
    resp_bad = await app_client.get(
        "/stats/summer-league/games?player=no-such-player-xyz"
    )
    assert resp_bad.status_code == 200
    assert "0 games" in resp_bad.text
    assert f"/stats/summer-league/2025/games/{g2025.id}" not in resp_bad.text


@pytest.mark.asyncio
async def test_game_box_score_renders_and_missing_404(
    app_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Box score shows both teams; an unknown game id 404s."""
    player = make_player("Box", "Scorer", school="UNC")
    db_session.add(player)
    await db_session.flush()
    game = await _seed_game(
        db_session,
        home_player=player,
        year=2025,
        league_id="15",
        venue_slug="las_vegas",
        game_date=date(2025, 7, 10),
    )

    # A DNP teammate (NULL minutes) on the home team: must sort BELOW played
    # lines despite Postgres placing NULLs first under DESC.
    dnp = make_player("Bench", "Warmer", school="Iowa")
    db_session.add(dnp)
    await db_session.flush()
    dnp_sp = await _source_player(db_session, player=dnp)
    assert game.home_team_entry_id is not None
    db_session.add(
        SummerLeaguePlayerGameLog(
            competition_id=game.competition_id,
            game_id=game.id,
            team_entry_id=game.home_team_entry_id,
            source_player_id=dnp_sp.id,
            player_id=dnp.id,
            nba_stats_person_id=dnp_sp.nba_stats_person_id,
            raw_player_name="Bench Warmer",
            minutes_seconds=None,
        )
    )
    # Both team game logs: the home box gives AST% its team context; the away
    # box completes the opponent context for rebound/steal/block %s + ORtg/DRtg.
    assert game.away_team_entry_id is not None
    db_session.add(
        SummerLeagueTeamGameLog(
            competition_id=game.competition_id,
            game_id=game.id,
            team_entry_id=game.home_team_entry_id,
            minutes=240,
            pts=100,
            fgm=40,
            fga=85,
            fg3m=10,
            fg3a=30,
            ftm=10,
            fta=15,
            oreb=12,
            dreb=28,
            reb=40,
            ast=22,
            stl=8,
            blk=4,
            tov=14,
            pf=18,
        )
    )
    db_session.add(
        SummerLeagueTeamGameLog(
            competition_id=game.competition_id,
            game_id=game.id,
            team_entry_id=game.away_team_entry_id,
            minutes=240,
            pts=90,
            fgm=36,
            fga=80,
            fg3m=8,
            fg3a=25,
            ftm=10,
            fta=14,
            oreb=10,
            dreb=30,
            reb=40,
            ast=20,
            stl=6,
            blk=3,
            tov=16,
            pf=20,
        )
    )
    await db_session.commit()
    assert game.id is not None

    box = await get_game_box_score(db_session, game.id)
    assert box is not None
    assert box.home.name == "Home Team"
    assert box.away.name == "Away Team"
    assert any(line.name == "Box Scorer" for line in box.home.lines)
    # DNP teammate renders last among home lines, not first.
    assert box.home.lines[-1].name == "Bench Warmer"
    assert box.home.lines[-1].dnp is True
    assert box.home.lines[0].dnp is False

    # Single-game advanced line: GmSc weights the full box (incl. OREB/DREB),
    # FTr/TOV% come from the player's own line, AST% from the team log context,
    # the rebound/steal/block rates + ORtg/DRtg from both team boxes.
    starter = box.home.lines[0]
    # 20 +0.4*8 -0.7*15 -0.4*(2-2) +0.7*3 +0.3*5 +1 +0.7*5 +0.7*1 -0.4*2 -2
    assert starter.gmsc == pytest.approx(18.7)
    assert starter.fg3ar == pytest.approx(round(5 / 15, 3))
    assert starter.ftr == pytest.approx(round(2 / 15, 3))
    assert starter.tov_pct == pytest.approx(round(100 * 2 / (15 + 0.44 * 2 + 2), 1))
    # AST% = 100*5 / ((30/48)*40 - 8) = 500/17 ≈ 29.4
    assert starter.ast_pct == pytest.approx(29.4)
    # ORB% = 100 * (3 * 48) / (30 * (12 + 30)) ≈ 11.4 against team+opp boards.
    assert starter.orb_pct == pytest.approx(round(100 * 3 * 48 / (30 * 42), 1))
    assert starter.trb_pct == pytest.approx(round(100 * 8 * 48 / (30 * 80), 1))
    assert starter.ortg is not None and 50 < starter.ortg < 200
    assert starter.drtg is not None and 50 < starter.drtg < 200
    # DNP line carries no advanced values.
    assert box.home.lines[-1].gmsc is None

    resp = await app_client.get(f"/stats/summer-league/2025/games/{game.id}")
    assert resp.status_code == 200
    assert "Box Scorer" in resp.text
    assert "Advanced" in resp.text
    # The full BBRef single-game advanced header set renders.
    for header in (
        ">GmSc<", ">3PAr<", ">FTr<", ">ORB%<", ">DRB%<", ">TRB%<",
        ">AST%<", ">STL%<", ">BLK%<", ">TOV%<", ">USG%<", ">ORtg<", ">DRtg<",
    ):
        assert header in resp.text
    assert "18.7" in resp.text
    assert "0.133" in resp.text
    assert "29.4" in resp.text

    missing = await app_client.get("/stats/summer-league/2025/games/99999999")
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_player_game_logs_grouped_and_unknown_404(
    app_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Player logs group by competition; an unknown slug 404s."""
    player = make_player("Logs", "Player", school="Kentucky")
    db_session.add(player)
    await db_session.flush()
    await _seed_game(
        db_session,
        home_player=player,
        year=2025,
        league_id="15",
        venue_slug="las_vegas",
        game_date=date(2025, 7, 9),
    )
    await db_session.commit()

    assert player.slug is not None
    resp = await app_client.get(f"/players/{player.slug}/summer-league")
    assert resp.status_code == 200
    assert "Logs Player" in resp.text
    assert "Las Vegas" in resp.text

    missing = await app_client.get("/players/no-such-player-xyz/summer-league")
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_player_detail_has_game_log_hook(
    app_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The player-detail SL section links to the game log and box scores."""
    player = make_player("Hook", "Player", school="Baylor")
    db_session.add(player)
    await db_session.flush()
    game = await _seed_game(
        db_session,
        home_player=player,
        year=2025,
        league_id="15",
        venue_slug="las_vegas",
        game_date=date(2025, 7, 8),
    )
    await db_session.commit()

    assert player.slug is not None and game.id is not None
    resp = await app_client.get(f"/players/{player.slug}")
    assert resp.status_code == 200
    assert f"/players/{player.slug}/summer-league" in resp.text
    assert f"/stats/summer-league/2025/games/{game.id}" in resp.text
