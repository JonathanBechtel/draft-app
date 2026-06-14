"""Integration tests for the Summer League leaders page.

- ``get_leaders`` ranks players per mode, scales counting stats, computes
  advanced rates, applies the min-sample filter, and scaffolds composite
  placeholders.
- The route renders for every mode.
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
from app.services.summer_league_leaders_service import get_leaders
from tests.integration.conftest import make_player

_N = {"i": 0}


async def _seed_player(
    db: AsyncSession,
    *,
    comp: SummerLeagueCompetition,
    team: SummerLeagueTeamEntry,
    player: PlayerMaster,
    n_games: int,
    pts: int,
    reb: int = 4,
    ast: int = 4,
) -> None:
    """Seed ``n_games`` identical played logs for one player."""
    _N["i"] += 1
    sp = SummerLeagueSourcePlayer(
        nba_stats_person_id=f"p-{_N['i']}",
        raw_player_name=player.display_name or "Player",
        normalized_name=(player.display_name or "player").lower(),
        canonical_player_id=player.id,
    )
    db.add(sp)
    await db.flush()
    assert comp.id is not None and team.id is not None
    for _g in range(n_games):
        _N["i"] += 1
        game = SummerLeagueGame(
            competition_id=comp.id,
            nba_stats_game_id=f"g-{_N['i']}",
            game_date=date(2025, 7, 5),
            home_team_entry_id=team.id,
            away_team_entry_id=team.id,
            home_score=100,
            away_score=90,
        )
        db.add(game)
        await db.flush()
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
                pace=100.0,
                pts=pts,
                reb=reb,
                ast=ast,
                fgm=pts // 3,
                fga=pts // 2,
                fg3m=1,
                fg3a=3,
                ftm=2,
                fta=2,
                ts_pct=0.6,
                efg_pct=0.55,
                usg_pct=0.25,
                pie=0.12,
                off_rating=110.0,
                def_rating=105.0,
                net_rating=5.0,
                reb_pct=0.1,
                ast_pct=0.2,
            )
        )
    await db.flush()


async def _seed(db: AsyncSession) -> None:
    comp = SummerLeagueCompetition(
        year=2025, league_id="15", venue_slug="las_vegas", display_name="2025 LV"
    )
    db.add(comp)
    await db.flush()
    assert comp.id is not None
    team = SummerLeagueTeamEntry(
        competition_id=comp.id,
        nba_stats_team_id="t1",
        raw_team_name="Team",
        raw_team_abbreviation="TM",
        team_slug="tm",
    )
    db.add(team)
    await db.flush()

    star = make_player("Star", "Scorer", school="Duke")
    role = make_player("Role", "Player", school="UNC")
    cup = make_player("One", "Gamer", school="Iowa")
    db.add_all([star, role, cup])
    await db.flush()
    await _seed_player(db, comp=comp, team=team, player=star, n_games=3, pts=30, ast=8)
    await _seed_player(db, comp=comp, team=team, player=role, n_games=3, pts=12, ast=2)
    await _seed_player(db, comp=comp, team=team, player=cup, n_games=1, pts=40)
    await db.commit()


@pytest.mark.asyncio
async def test_leaders_modes_ranking_and_filter(
    app_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Per-game and totals modes rank correctly; min-sample filters the 1-gamer."""
    await _seed(db_session)

    # Per game by PTS: star (30) first, role (12) second; the 1-game player is
    # excluded by the default 2-GP minimum.
    pg = await get_leaders(db_session, mode="per_game", sort="pts")
    assert [r.name for r in pg.rows] == ["Star Scorer", "Role Player"]
    assert pg.rows[0].values["pts"] == pytest.approx(30.0)
    assert pg.rows[0].rank == 1

    # Totals scale up: star 90 PTS.
    tot = await get_leaders(db_session, mode="totals", sort="pts")
    assert tot.rows[0].values["pts"] == 90

    # Re-sort by assists: star (8) still leads role (2).
    by_ast = await get_leaders(db_session, mode="per_game", sort="ast")
    assert by_ast.rows[0].name == "Star Scorer"
    assert by_ast.sort == "ast"

    # Advanced mode exposes rate stats and scaffolds composite placeholders.
    adv = await get_leaders(db_session, mode="advanced")
    keys = {c.key for c in adv.columns}
    assert {"ts_pct", "usg_pct", "pie"} <= keys
    placeholders = {c.key for c in adv.columns if c.placeholder}
    assert placeholders == {"gamescore", "sl_score"}
    assert adv.rows[0].values["ts_pct"] is not None
    assert adv.rows[0].values["gamescore"] is None


@pytest.mark.asyncio
async def test_leaders_route_renders_each_mode(
    app_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The leaders route renders for every display mode."""
    await _seed(db_session)
    for mode in ("totals", "per_game", "per_36", "per_100", "advanced"):
        resp = await app_client.get(f"/stats/summer-league/leaders?mode={mode}")
        assert resp.status_code == 200
        assert "Leaders" in resp.text
        assert "Star Scorer" in resp.text
