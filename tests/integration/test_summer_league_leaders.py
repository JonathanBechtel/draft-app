"""Integration tests for the Summer League leaders page.

- ``get_leaders`` ranks players per counting mode, scales counting stats, and
  applies the min-sample filter.
- ``advanced`` mode is per-competition and reads the materialized metrics table
  (PER/WS/BPM/VORP), defaulting to the latest competition and scoping by venue.
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
from app.schemas.summer_league_metrics import SummerLeaguePlayerSeason
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

    # Full box line: shooting volume + split rebounds, fouls, eFG%, plus-minus.
    col_keys = {c.key for c in pg.columns}
    assert {"fgm", "fga", "fg3m", "fg3a", "ftm", "fta"} <= col_keys
    assert {"oreb", "dreb", "pf", "efg_pct", "plus_minus"} <= col_keys
    assert "fgm" in pg.rows[0].values
    assert "efg_pct" in pg.rows[0].values

    # Totals scale up: star 90 PTS.
    tot = await get_leaders(db_session, mode="totals", sort="pts")
    assert tot.rows[0].values["pts"] == 90

    # Re-sort by assists: star (8) still leads role (2).
    by_ast = await get_leaders(db_session, mode="per_game", sort="ast")
    assert by_ast.rows[0].name == "Star Scorer"
    assert by_ast.sort == "ast"

    # Advanced mode is per-competition (composite columns, no placeholders) and
    # reads the metrics table — empty here since no player_seasons were seeded.
    adv = await get_leaders(db_session, mode="advanced")
    keys = {c.key for c in adv.columns}
    assert {"per", "ws", "bpm", "vorp"} <= keys
    # Broadened advanced suite: split rates, offensive/defensive splits, GmSc.
    assert {
        "orb_pct",
        "drb_pct",
        "stl_pct",
        "blk_pct",
        "tov_pct",
        "obpm",
        "dbpm",
        "ows",
        "dws",
        "ws40",
        "gmsc",
    } <= keys
    assert all(not c.placeholder for c in adv.columns)
    assert adv.is_advanced is True
    assert adv.rows == []


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
        # The counting modes list the seeded game-log players; advanced reads the
        # (here unseeded) metrics table, so it renders but lists no players.
        if mode != "advanced":
            assert "Star Scorer" in resp.text


async def _comp(
    db: AsyncSession, *, year: int, venue: str, league_id: str
) -> SummerLeagueCompetition:
    comp = SummerLeagueCompetition(
        year=year, league_id=league_id, venue_slug=venue, display_name=f"{year} {venue}"
    )
    db.add(comp)
    await db.flush()
    return comp


async def _season(
    db: AsyncSession,
    *,
    comp: SummerLeagueCompetition,
    first: str,
    last: str,
    adv_eligible: bool = True,
    minutes: float = 120.0,
    gp: int = 3,
    **metrics: float,
) -> None:
    player = make_player(first, last)
    db.add(player)
    await db.flush()
    db.add(
        SummerLeaguePlayerSeason(
            competition_id=comp.id,
            player_id=player.id,
            year=comp.year,
            venue_slug=comp.venue_slug,
            gp=gp,
            minutes=minutes,
            adv_eligible=adv_eligible,
            **metrics,
        )
    )
    await db.flush()


@pytest.mark.asyncio
async def test_advanced_leaders_from_metrics_table(
    app_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Advanced mode ranks a competition's players from the materialized table,
    defaulting to the latest competition, scoping by venue, and gating rows."""
    lv25 = await _comp(db_session, year=2025, venue="las_vegas", league_id="15")
    slc24 = await _comp(db_session, year=2024, venue="salt_lake_city", league_id="16")

    await _season(db_session, comp=lv25, first="Vegas", last="Ace", per=30.0, ws=2.0)
    await _season(db_session, comp=lv25, first="Vegas", last="Role", per=20.0, ws=1.0)
    # Excluded: under the 40-min display floor.
    await _season(
        db_session, comp=lv25, first="Cup", last="Coffee", minutes=30.0, gp=1, per=99.0
    )
    # Excluded: pool not adv-eligible.
    await _season(
        db_session, comp=lv25, first="Thin", last="Pool", adv_eligible=False, per=88.0
    )
    await _season(db_session, comp=slc24, first="Salt", last="Laker", per=18.0)
    await db_session.commit()

    # Defaults to the latest competition (2025 Las Vegas), ranked by PER desc,
    # excluding the sub-floor and non-eligible players.
    adv = await get_leaders(db_session, mode="advanced")
    assert adv.year == 2025 and adv.venue == "las_vegas"
    assert [r.name for r in adv.rows] == ["Vegas Ace", "Vegas Role"]
    assert adv.rows[0].values["per"] == 30.0
    assert {v[0] for v in adv.venues} == {"las_vegas", "salt_lake_city"}

    # Venue scoping resolves a different competition.
    slc = await get_leaders(
        db_session, mode="advanced", year=2024, venue="salt_lake_city"
    )
    assert slc.year == 2024 and slc.venue == "salt_lake_city"
    assert [r.name for r in slc.rows] == ["Salt Laker"]

    # Sorting by a different metric re-ranks within the competition.
    by_ws = await get_leaders(db_session, mode="advanced", sort="ws")
    assert by_ws.sort == "ws"
    assert by_ws.rows[0].name == "Vegas Ace"
