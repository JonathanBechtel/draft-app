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
    seconds: int = 1800,
) -> None:
    """Seed ``n_games`` identical played logs (``seconds`` each) for one player."""
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
                minutes_seconds=seconds,
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


async def _seed_two_venues(db: AsyncSession) -> None:
    """Seed one player at Las Vegas and another at Salt Lake City, same year."""
    lv = SummerLeagueCompetition(
        year=2025, league_id="15", venue_slug="las_vegas", display_name="2025 LV"
    )
    slc = SummerLeagueCompetition(
        year=2025, league_id="16", venue_slug="salt_lake_city", display_name="2025 SLC"
    )
    db.add_all([lv, slc])
    await db.flush()
    assert lv.id is not None and slc.id is not None
    lv_team = SummerLeagueTeamEntry(
        competition_id=lv.id,
        nba_stats_team_id="lv1",
        raw_team_name="Vegas",
        raw_team_abbreviation="LV",
        team_slug="lv",
    )
    slc_team = SummerLeagueTeamEntry(
        competition_id=slc.id,
        nba_stats_team_id="slc1",
        raw_team_name="Salt",
        raw_team_abbreviation="SLC",
        team_slug="slc",
    )
    db.add_all([lv_team, slc_team])
    await db.flush()
    vegas = make_player("Vegas", "Baller", school="UNLV")
    salt = make_player("Salt", "Laker", school="Utah")
    db.add_all([vegas, salt])
    await db.flush()
    await _seed_player(db, comp=lv, team=lv_team, player=vegas, n_games=3, pts=25)
    await _seed_player(db, comp=slc, team=slc_team, player=salt, n_games=3, pts=20)
    await db.commit()


@pytest.mark.asyncio
async def test_leaders_venue_filter_scopes_counting_mode(
    app_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """A venue filter scopes counting modes to one competition; unset blends all."""
    await _seed_two_venues(db_session)

    # No venue: both players appear (blended across venues).
    both = await get_leaders(db_session, mode="per_game", sort="pts")
    assert {r.name for r in both.rows} == {"Vegas Baller", "Salt Laker"}
    # The picker lists both seeded venues, marquee-first (Las Vegas before SLC).
    assert both.venues == [
        ("las_vegas", "Las Vegas"),
        ("salt_lake_city", "Salt Lake City"),
    ]
    assert both.venue is None

    # Scoped to Las Vegas: only the LV player, and the resolved state reflects it.
    lv = await get_leaders(db_session, mode="per_game", sort="pts", venue="las_vegas")
    assert [r.name for r in lv.rows] == ["Vegas Baller"]
    assert lv.venue == "las_vegas"
    assert lv.venue_label == "Las Vegas"

    # A bogus venue slug is ignored (treated as "All venues").
    bogus = await get_leaders(db_session, mode="per_game", venue="atlantis")
    assert bogus.venue is None
    assert len(bogus.rows) == 2


@pytest.mark.asyncio
async def test_leaders_empty_state_soft_landing(
    app_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """When filters exclude everyone, the page renders a soft-landing message."""
    await _seed_two_venues(db_session)
    # Minimums no one can meet → zero rows.
    resp = await app_client.get(
        "/stats/summer-league/leaders?mode=per_game&min_gp=99&min_min=9999"
    )
    assert resp.status_code == 200
    assert "No players match these filters" in resp.text
    assert "Reset filters" in resp.text
    # The friendly copy should echo the active filter context.
    assert "99+ GP" in resp.text


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
    """Advanced mode reads the materialized table: a specific season+venue ranks
    one pool-calibrated competition; leaving either open blends the pools."""
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
    await _season(
        db_session, comp=slc24, first="Salt", last="Laker", per=18.0, ftr=0.45
    )
    await db_session.commit()

    # Default (no season/venue) is the all-time, all-venue blend across pools,
    # ranked by PER desc, excluding the sub-floor and non-eligible players.
    adv = await get_leaders(db_session, mode="advanced")
    assert adv.year is None and adv.venue is None
    assert [r.name for r in adv.rows] == ["Vegas Ace", "Vegas Role", "Salt Laker"]
    assert adv.rows[0].values["per"] == 30.0
    assert {v[0] for v in adv.venues} == {"las_vegas", "salt_lake_city"}

    # A specific season + venue ranks that single competition only.
    slc = await get_leaders(
        db_session, mode="advanced", year=2024, venue="salt_lake_city"
    )
    assert slc.year == 2024 and slc.venue == "salt_lake_city"
    assert [r.name for r in slc.rows] == ["Salt Laker"]
    # Attempt-rate columns are on the advanced board and read the stored value.
    assert {c.key: c.label for c in slc.columns}["fg3ar"] == "3PAr"
    assert {c.key: c.label for c in slc.columns}["ftr"] == "FTr"
    assert slc.rows[0].values["ftr"] == 0.45

    # Sorting by a different metric re-ranks the board.
    by_ws = await get_leaders(db_session, mode="advanced", sort="ws")
    assert by_ws.sort == "ws"
    assert by_ws.rows[0].name == "Vegas Ace"


@pytest.mark.asyncio
async def test_advanced_all_blend_math(
    app_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The blended Advanced view sums accumulations and minute-weights rates."""
    lv25 = await _comp(db_session, year=2025, venue="las_vegas", league_id="15")
    slc25 = await _comp(db_session, year=2025, venue="salt_lake_city", league_id="16")

    # One player with two adv-eligible pools in 2025 (Vegas + Salt Lake), each
    # carrying shot volume so the pooled shooting percentages are exercised.
    star = make_player("Two", "Pooler")
    db_session.add(star)
    await db_session.flush()
    pools = (
        # comp, minutes, per, ws, pts, fga, fgm, fg3m, fg3a, fta
        (lv25, 100.0, 30.0, 2.0, 140, 100, 50, 20, 40, 20),
        (slc25, 100.0, 20.0, 1.0, 110, 100, 40, 10, 20, 10),
    )
    for comp, minutes, per, ws, pts, fga, fgm, fg3m, fg3a, fta in pools:
        db_session.add(
            SummerLeaguePlayerSeason(
                competition_id=comp.id,
                player_id=star.id,
                year=comp.year,
                venue_slug=comp.venue_slug,
                gp=3,
                minutes=minutes,
                adv_eligible=True,
                per=per,
                ws=ws,
                pts=pts,
                fga=fga,
                fgm=fgm,
                fg3m=fg3m,
                fg3a=fg3a,
                fta=fta,
            )
        )
    await db_session.flush()
    await db_session.commit()

    # 2025 across all venues blends the two pools into one line.
    blended = await get_leaders(db_session, mode="advanced", year=2025)
    assert blended.year == 2025 and blended.venue is None
    assert blended.venue_label == "All venues"
    assert [r.name for r in blended.rows] == ["Two Pooler"]
    row = blended.rows[0]
    assert row.gp == 6  # games summed
    assert row.values["min"] == 200.0  # minutes summed
    assert row.values["ws"] == 3.0  # accumulations summed
    # PER is minute-weighted: (30·100 + 20·100) / 200 = 25.0
    assert row.values["per"] == 25.0
    # WS/40 recomputed from summed shares: 3.0 / 200 · 40 = 0.6
    assert row.values["ws40"] == 0.6
    # TS% pools raw volume: 100·250 / (2·(200 + 0.44·30)) = 58.6
    assert row.values["ts_pct"] == 58.6
    # eFG% pools raw volume: 100·(90 + 0.5·30) / 200 = 52.5
    assert row.values["efg_pct"] == 52.5
    # Attempt rates pool raw volume as 0-1 fractions: 3PAr 60/200, FTr 30/200.
    assert row.values["fg3ar"] == 0.3
    assert row.values["ftr"] == 0.15


async def _seed_young_competition(db: AsyncSession) -> None:
    """Seed a mid-event venue: every player has exactly one played game.

    Nobody meets the standard 2+ GP / 60+ MIN gate; one player clears 20
    minutes, one doesn't.
    """
    comp = SummerLeagueCompetition(
        year=2026, league_id="16", venue_slug="salt_lake_city", display_name="2026 SLC"
    )
    db.add(comp)
    await db.flush()
    assert comp.id is not None
    team = SummerLeagueTeamEntry(
        competition_id=comp.id,
        nba_stats_team_id="slc-t1",
        raw_team_name="Salt",
        raw_team_abbreviation="SLC",
        team_slug="slc",
    )
    db.add(team)
    await db.flush()
    starter = make_player("Day", "One")
    benchie = make_player("Short", "Stint")
    db.add_all([starter, benchie])
    await db.flush()

    # 25 minutes / 10 minutes — one game each.
    await _seed_player(
        db, comp=comp, team=team, player=starter, n_games=1, pts=22, seconds=1500
    )
    await _seed_player(
        db, comp=comp, team=team, player=benchie, n_games=1, pts=5, seconds=600
    )
    await db.commit()


@pytest.mark.asyncio
async def test_auto_gates_relax_for_young_competition(
    app_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Unpinned thresholds walk the gate ladder so a mid-event board populates.

    With every player at 1 GP the standard 2+ GP / 60+ MIN rung matches nobody;
    the (1, 20) rung catches the 25-minute player. Explicit thresholds are
    still honored exactly.
    """
    await _seed_young_competition(db_session)

    auto = await get_leaders(db_session, mode="per_game", sort="pts")
    assert [r.name for r in auto.rows] == ["Day One"]
    assert auto.auto_gates is True
    assert auto.gates_relaxed is True
    assert (auto.min_games, auto.min_minutes) == (1, 20)

    # The floor rung (1, 0) applies when even 20 minutes is too strict.
    floor = await get_leaders(
        db_session, mode="per_game", sort="pts", min_games=1, min_minutes=0
    )
    assert {r.name for r in floor.rows} == {"Day One", "Short Stint"}
    assert floor.auto_gates is False
    assert floor.gates_relaxed is False

    # Explicit standard gates are honored even when they match nobody.
    pinned = await get_leaders(
        db_session, mode="per_game", sort="pts", min_games=2, min_minutes=60
    )
    assert pinned.rows == []
    assert pinned.auto_gates is False
    assert (pinned.min_games, pinned.min_minutes) == (2, 60)


@pytest.mark.asyncio
async def test_leaders_route_auto_gates_ui(
    app_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The route defaults to adaptive gates and renders the relaxed-gate note.

    Explicit query gates keep the strict empty state.
    """
    await _seed_young_competition(db_session)

    resp = await app_client.get("/stats/summer-league/leaders?mode=per_game")
    assert resp.status_code == 200
    assert "Day One" in resp.text
    assert "Early-competition view" in resp.text

    # Pinned gates that match nobody keep the honest filter empty state.
    strict = await app_client.get(
        "/stats/summer-league/leaders?mode=per_game&min_gp=2&min_min=60"
    )
    assert strict.status_code == 200
    assert "No players match these filters" in strict.text

    # Unparseable gate values fall back to adaptive rather than erroring.
    junk = await app_client.get(
        "/stats/summer-league/leaders?mode=per_game&min_gp=abc&min_min="
    )
    assert junk.status_code == 200
    assert "Day One" in junk.text


@pytest.mark.asyncio
async def test_leaders_route_no_data_empty_state(
    app_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """A venue with a competition but no played games gets the pre-tipoff copy."""
    comp = SummerLeagueCompetition(
        year=2026, league_id="15", venue_slug="las_vegas", display_name="2026 LV"
    )
    db_session.add(comp)
    await db_session.commit()

    resp = await app_client.get(
        "/stats/summer-league/leaders?mode=per_game&year=2026&venue=las_vegas"
    )
    assert resp.status_code == 200
    assert "No game data yet" in resp.text


@pytest.mark.asyncio
async def test_advanced_uncalibrated_competition_is_honored(
    app_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """An exactly-requested pool that isn't adv-eligible yet is honored.

    It shows its own rows (box-derived rates, GmSc default sort) instead of
    redirecting to another competition.
    """
    cc = await _comp(db_session, year=2026, venue="california_classic", league_id="13")
    slc = await _comp(db_session, year=2026, venue="salt_lake_city", league_id="16")

    # Calibrated pool: normal advanced board.
    await _season(db_session, comp=cc, first="Cali", last="Classic", per=22.0, ws=1.5)
    # Mid-event pool: below the display floor, composites unset, GmSc live.
    await _season(
        db_session,
        comp=slc,
        first="Salt",
        last="Sprinter",
        adv_eligible=False,
        minutes=30.0,
        gp=1,
        gmsc=14.5,
        ts_pct=61.0,
    )
    await _season(
        db_session,
        comp=slc,
        first="Salt",
        last="Stroller",
        adv_eligible=False,
        minutes=25.0,
        gp=1,
        gmsc=6.0,
    )
    await db_session.commit()

    adv = await get_leaders(
        db_session, mode="advanced", year=2026, venue="salt_lake_city"
    )
    # The requested competition is honored, not redirected to California Classic.
    assert adv.year == 2026 and adv.venue == "salt_lake_city"
    assert adv.uncalibrated is True
    assert adv.sort == "gmsc"
    assert [r.name for r in adv.rows] == ["Salt Sprinter", "Salt Stroller"]
    assert adv.rows[0].values["gmsc"] == 14.5
    assert adv.rows[0].values["ts_pct"] == 61.0
    assert adv.rows[0].values["per"] is None  # composite gated until calibration
    # The venue picker includes the uncalibrated selection so controls read right.
    assert ("salt_lake_city", "Salt Lake City") in adv.venues

    # The calibrated pool is untouched by the new path.
    cal = await get_leaders(
        db_session, mode="advanced", year=2026, venue="california_classic"
    )
    assert cal.uncalibrated is False
    assert cal.sort == "per"
    assert [r.name for r in cal.rows] == ["Cali Classic"]

    # Route render includes the calibration note.
    resp = await app_client.get(
        "/stats/summer-league/leaders?mode=advanced&year=2026&venue=salt_lake_city"
    )
    assert resp.status_code == 200
    assert "calibrated yet" in resp.text
    assert "Salt Sprinter" in resp.text


@pytest.mark.asyncio
async def test_venue_mini_leaders_relax_to_one_game(
    app_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Venue-page mini leaderboards fall back to 1+ GP mid-event.

    Instead of rendering "No qualified players".
    """
    from app.services.summer_league_season_service import get_venue_leaders

    await _seed_young_competition(db_session)
    leaders = await get_venue_leaders(db_session, 2026, "salt_lake_city")
    names = [r.name for r in leaders.pts]
    assert "Day One" in names and "Short Stint" in names
