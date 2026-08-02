"""Golden-number parity harness for the Summer League stat engine (Phase 2, T1).

This is the gate for the whole Phase 2 consolidation
(``docs/plans/summer-league-phase2-stat-engine-tickets.md``): ten downstream
tickets delete duplicated formula copies and repoint call sites at the single
engine in ``app/services/summer_league/metrics.py``. This test is the only
thing that will notice if one of those deletions quietly changes a
user-visible number.

For one deterministic, hand-built competition it asserts, for a fixed player::

    engine value  ==  stored column  ==  Explorer cell  ==  leaderboard value

across the four independently-computed surfaces:

* **engine**    -- ``compute()`` called directly (in-memory, pre-persistence).
* **stored**    -- ``rebuild()``'s materialized ``SummerLeaguePlayerSeason`` row.
* **Explorer**  -- ``run_explorer_query()`` (the real read path the page uses),
  ``subject="players"``, ``grain="per_competition"``.
* **leaderboard** -- ``get_leaders(mode="advanced", ...)`` (the real read path
  the leaders page uses).

Metrics covered span all three rollup classes named in the ticket
(``rollup_additive`` / ``rollup_rate_composite`` / ``rollup_recombinable`` in
``summer_league_explorer_service.py``):

* recombinable  -- TS%, eFG%, TOV%, 3PAr, FTr, Game Score
* pool-recalibrated (rate composite) -- PER
* additive-share -- Win Shares (WS)

plus the per-36 / per-100 scaled forms on the Explorer and counting leaderboard.

**Every expected value below is a literal**, not something this test derives
by calling the formula under test. TS%/eFG%/TOV%/3PAr/FTr/Game Score are
worked out by hand from the box totals (see the docstring on each test).
PER/WS/pace/pts-per-100 are not practical to hand-derive (PER rides a
per-pool standardization scalar, WS a Pythagorean-fit coefficient, pace/pts
per-100 an estimated-possessions formula) -- those literals were captured by
running this exact fixture once against the current implementation and
reviewed for plausibility (PER standardizing to the invariant mean of 15.0
for a uniform-box pool is a structural check independent of the fit; WS/pace
were sanity-checked for order of magnitude and internal consistency between
surfaces). A later ticket that changes one of these numbers is the signal
this harness exists to catch, not a reason to update the literal to match.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeagueGame,
    SummerLeaguePlayerGameLog,
    SummerLeagueSourcePlayer,
    SummerLeagueTeamEntry,
    SummerLeagueTeamGameLog,
)
from app.schemas.summer_league_metrics import SummerLeaguePlayerSeason
from app.services.summer_league.metrics import compute, rebuild
from app.services.summer_league_explorer_service import (
    ExplorerQuery,
    run_explorer_query,
)
from app.services.summer_league_leaders_service import get_leaders
from tests.integration.conftest import make_player

_N = {"i": 0}

# One player's per-game box line. Twelve players (two 6-player teams) times
# four games each gives an advanced-eligible pool (>=10 qualified players,
# 100% complete-box games) with small enough numbers to hand-verify the
# box-derived ratios. Every player gets the identical line, so any player's
# aggregate is the same and PER standardizes to exactly the pool mean (15.0)
# regardless of the underlying (unfitted, n<20 -> 13.0 fallback) coefficients.
_LINE = dict(
    minutes_seconds=1800,  # 30 minutes
    pts=12,
    fgm=5,
    fga=10,
    fg3m=1,
    fg3a=3,
    ftm=1,
    fta=2,
    oreb=1,
    dreb=3,
    reb=4,
    ast=2,
    stl=1,
    blk=1,
    tov=2,
    pf=2,
)
_N_GAMES = 4
_PLAYERS_PER_TEAM = 6

_YEAR = 2025
_VENUE = "las_vegas"


async def _team(db: AsyncSession, comp_id: int, idx: int) -> SummerLeagueTeamEntry:
    _N["i"] += 1
    team = SummerLeagueTeamEntry(
        competition_id=comp_id,
        nba_stats_team_id=f"t-{_N['i']}",
        raw_team_name=f"Team {idx}",
        raw_team_abbreviation=f"T{idx}",
        team_slug=f"team-{_N['i']}",
    )
    db.add(team)
    await db.flush()
    assert team.id is not None
    return team


async def _players(db: AsyncSession, n: int) -> list:
    out = []
    for _i in range(n):
        _N["i"] += 1
        p = make_player(f"Parity{_N['i']}", f"Player{_N['i']}")
        db.add(p)
        await db.flush()
        sp = SummerLeagueSourcePlayer(
            nba_stats_person_id=f"sp-{_N['i']}",
            raw_player_name=p.display_name or "P",
            normalized_name=(p.display_name or "p").lower(),
            canonical_player_id=p.id,
        )
        db.add(sp)
        await db.flush()
        out.append((p, sp))
    return out


async def _seed_fixture(db: AsyncSession) -> tuple[int, str]:
    """Seed one advanced-eligible pool; return ``(competition_id, target_player_slug)``.

    Target player is roster_a[0] -- an even ``plus_minus`` index (see loop
    below), so plus-minus totals +8 over the 4 games (not asserted on here,
    but keeps the fixture's shape self-documenting for anyone extending it).
    """
    comp = SummerLeagueCompetition(
        year=_YEAR,
        league_id="15",
        venue_slug=_VENUE,
        display_name=f"{_YEAR} {_VENUE}",
    )
    db.add(comp)
    await db.flush()
    assert comp.id is not None
    team_a = await _team(db, comp.id, 1)
    team_b = await _team(db, comp.id, 2)
    roster_a = await _players(db, _PLAYERS_PER_TEAM)
    roster_b = await _players(db, _PLAYERS_PER_TEAM)

    n = _PLAYERS_PER_TEAM
    team_total = {k: v * n for k, v in _LINE.items() if k != "minutes_seconds"}
    team_minutes = (_LINE["minutes_seconds"] // 60) * n

    for g in range(_N_GAMES):
        _N["i"] += 1
        home, away = (team_a, team_b) if g % 2 == 0 else (team_b, team_a)
        game = SummerLeagueGame(
            competition_id=comp.id,
            nba_stats_game_id=f"g-{_N['i']}",
            game_date=date(_YEAR, 7, 6),
            home_team_entry_id=home.id,
            away_team_entry_id=away.id,
            home_score=80,
            away_score=72,
        )
        db.add(game)
        await db.flush()
        for team, roster in ((team_a, roster_a), (team_b, roster_b)):
            db.add(
                SummerLeagueTeamGameLog(
                    competition_id=comp.id,
                    game_id=game.id,
                    team_entry_id=team.id,
                    minutes=team_minutes,
                    **team_total,
                )
            )
            for pid, (player, sp) in enumerate(roster):
                db.add(
                    SummerLeaguePlayerGameLog(
                        competition_id=comp.id,
                        game_id=game.id,
                        team_entry_id=team.id,
                        source_player_id=sp.id,
                        player_id=player.id,
                        nba_stats_person_id=sp.nba_stats_person_id,
                        raw_player_name=player.display_name or "P",
                        plus_minus=(2 if pid % 2 == 0 else -2),
                        **_LINE,
                    )
                )
    await db.flush()

    target_player, _sp = roster_a[0]
    assert target_player.slug is not None
    return comp.id, target_player.slug


# --------------------------------------------------------------------------- #
# Literal expected values -- see the module docstring for provenance.
# --------------------------------------------------------------------------- #
# Box totals behind the recombinable metrics (identical for every player):
#   pts=48 fgm=20 fga=40 fg3m=4 fg3a=12 ftm=4 fta=8 tov=8, gp=4, minutes=120.
EXPECTED_TS_PCT = 55.1  # 100*48 / (2*(40+0.44*8))
EXPECTED_EFG_PCT = 55.0  # 100*(20+0.5*4) / 40
EXPECTED_TOV_PCT = 15.5  # 100*8 / (40+0.44*8+8)
EXPECTED_FG3AR = 0.3  # 12/40
EXPECTED_FTR = 0.2  # 8/40
EXPECTED_GMSC_PER_GAME = 8.5  # game_score(totals)/gp = 34.0/4
EXPECTED_GMSC_TOTALS = 34  # round(game_score(totals))
EXPECTED_GMSC_PER_36 = 10.2  # 34.0 * 36/120
EXPECTED_PTS_PER_36 = 14.4  # 48 * 36/120

# Pool-recalibrated: PER standardizes to the pool's minute-weighted mean by
# construction (`ps.metrics["per"] = round(aper * 15/scalar, 1)`); every
# player here has an identical box, so every player's aPER is the scalar
# itself and PER == 15.0 exactly -- a structural invariant, not a fitted
# number, which is why it is safe to pin without a captured run.
EXPECTED_PER = 15.0

# Additive-share (WS) and the possession-derived pace/pts-per-100 are not
# practical to hand-derive (Pythagorean-fit WS coefficient; possession
# estimate with the 1.07 OREB constant) -- captured once from this fixture.
EXPECTED_WS = 0.34
EXPECTED_PACE = 91.7
EXPECTED_PTS_PER100 = 20.9


@pytest.mark.asyncio
async def test_engine_stored_explorer_and_leaderboard_agree_on_recombinable_metrics(
    db_session: AsyncSession,
) -> None:
    """TS%/eFG%/TOV%/3PAr/FTr/GmSc: engine == stored == Explorer == leaderboard.

    These are the "recombinable" class -- computed straight from box totals,
    no league context needed -- so they must agree bit-for-bit across all
    four surfaces for the fixed player+competition in this fixture.
    """
    comp_id, slug = await _seed_fixture(db_session)
    await db_session.commit()

    # --- engine: compute() in memory, before any persistence.
    target = (
        await db_session.execute(select(PlayerMaster).where(PlayerMaster.slug == slug))
    ).scalar_one()
    result = await compute(db_session)
    engine_ps = next(
        ps
        for ps in result.seasons
        if ps.competition_id == comp_id and ps.player_id == target.id
    )
    engine_m = engine_ps.metrics
    assert engine_m["ts_pct"] == EXPECTED_TS_PCT
    assert engine_m["efg_pct"] == EXPECTED_EFG_PCT
    assert engine_m["tov_pct"] == EXPECTED_TOV_PCT
    assert engine_m["fg3ar"] == EXPECTED_FG3AR
    assert engine_m["ftr"] == EXPECTED_FTR
    assert engine_m["gmsc"] == EXPECTED_GMSC_PER_GAME

    # --- stored: rebuild() persists a fresh materialization. compute() above
    # already autobegan a transaction on this session (it issues reads), so
    # rebuild()'s writes join that transaction rather than opening a new one.
    await rebuild(db_session)
    await db_session.commit()
    stored = (
        await db_session.execute(
            select(SummerLeaguePlayerSeason).where(
                SummerLeaguePlayerSeason.competition_id == comp_id,
                SummerLeaguePlayerSeason.player_id == target.id,
                SummerLeaguePlayerSeason.is_current.is_(True),  # type: ignore[attr-defined]
            )
        )
    ).scalar_one()
    assert stored.ts_pct == EXPECTED_TS_PCT
    assert stored.efg_pct == EXPECTED_EFG_PCT
    assert stored.tov_pct == EXPECTED_TOV_PCT
    assert stored.fg3ar == EXPECTED_FG3AR
    assert stored.ftr == EXPECTED_FTR
    assert stored.gmsc == EXPECTED_GMSC_PER_GAME

    # --- Explorer: the real read path (subject="players", per_competition grain).
    explorer_result = await run_explorer_query(
        db_session,
        ExplorerQuery(
            subject="players",
            grain="per_competition",
            year_min=_YEAR,
            year_max=_YEAR,
            venue=_VENUE,
            mode="per_game",
            player_slug=slug,
            min_games=1,
            min_minutes=1,
        ),
    )
    explorer_row = explorer_result.rows[0]
    assert explorer_row.values["ts_pct"] == EXPECTED_TS_PCT
    assert explorer_row.values["efg_pct"] == EXPECTED_EFG_PCT
    assert explorer_row.values["tov_pct"] == EXPECTED_TOV_PCT
    assert explorer_row.values["fg3ar"] == EXPECTED_FG3AR
    assert explorer_row.values["ftr"] == EXPECTED_FTR
    assert explorer_row.values["gmsc"] == EXPECTED_GMSC_PER_GAME

    # --- leaderboard: the real read path (mode="advanced", single competition).
    leaders_result = await get_leaders(
        db_session,
        mode="advanced",
        year=_YEAR,
        venue=_VENUE,
        min_games=1,
        min_minutes=1,
    )
    leader_row = next(r for r in leaders_result.rows if r.slug == slug)
    assert leader_row.values["ts_pct"] == EXPECTED_TS_PCT
    assert leader_row.values["efg_pct"] == EXPECTED_EFG_PCT
    assert leader_row.values["tov_pct"] == EXPECTED_TOV_PCT
    assert leader_row.values["fg3ar"] == EXPECTED_FG3AR
    assert leader_row.values["ftr"] == EXPECTED_FTR
    assert leader_row.values["gmsc"] == EXPECTED_GMSC_PER_GAME


@pytest.mark.asyncio
async def test_default_explorer_reads_current_snapshot_and_exposes_source_currency(
    db_session: AsyncSession,
) -> None:
    """Default career output matches engine/stored values and labels its watermark.

    This is the Phase 3 read-switch leg: the same seeded competition is observed
    through the live engine, the current materialized season row, and the
    default Explorer grain.  A per-game query remains explicitly live and does
    not inherit the snapshot's ``as_of`` value.
    """
    comp_id, slug = await _seed_fixture(db_session)
    await db_session.commit()
    engine_result = await compute(db_session)
    target = (
        await db_session.execute(select(PlayerMaster).where(PlayerMaster.slug == slug))
    ).scalar_one()
    engine_ps = next(
        ps
        for ps in engine_result.seasons
        if ps.competition_id == comp_id and ps.player_id == target.id
    )

    await rebuild(db_session)
    await db_session.commit()
    stored = (
        await db_session.execute(
            select(SummerLeaguePlayerSeason).where(
                SummerLeaguePlayerSeason.competition_id == comp_id,
                SummerLeaguePlayerSeason.player_id == target.id,
                SummerLeaguePlayerSeason.is_current.is_(True),  # type: ignore[attr-defined]
            )
        )
    ).scalar_one()

    default = await run_explorer_query(
        db_session,
        ExplorerQuery(subject="players", player_slug=slug),
    )
    row = next(r for r in default.rows if r.href == f"/players/{slug}")
    assert default.read_source == "snapshot"
    assert default.as_of == stored.as_of
    assert row.values["pts"] == engine_ps.box.pts / engine_ps.box.gp
    assert stored.pts / stored.gp == engine_ps.box.pts / engine_ps.box.gp
    assert row.values["efg_pct"] == engine_ps.metrics["efg_pct"] == stored.efg_pct
    assert row.values["gmsc"] == engine_ps.metrics["gmsc"]

    live = await run_explorer_query(
        db_session,
        ExplorerQuery(subject="players", grain="per_game", min_games=1),
    )
    assert live.read_source == "live"
    assert live.as_of is None


@pytest.mark.asyncio
async def test_engine_stored_explorer_and_leaderboard_agree_on_pool_recalibrated_and_additive_metrics(
    db_session: AsyncSession,
) -> None:
    """PER (pool-recalibrated) and WS (additive-share): all four surfaces agree.

    Unlike the recombinable metrics, these need a real, adv-eligible league
    context -- this fixture's pool qualifies (12 players >= ADV_MIN_PLAYERS,
    100% complete-box games >= ADV_MIN_COMPLETE_FRAC).
    """
    comp_id, slug = await _seed_fixture(db_session)
    await db_session.commit()

    target = (
        await db_session.execute(select(PlayerMaster).where(PlayerMaster.slug == slug))
    ).scalar_one()

    result = await compute(db_session)
    engine_ps = next(
        ps
        for ps in result.seasons
        if ps.competition_id == comp_id and ps.player_id == target.id
    )
    assert engine_ps.metrics["per"] == EXPECTED_PER
    assert engine_ps.metrics["ws"] == EXPECTED_WS
    assert engine_ps.metrics["pace"] == EXPECTED_PACE
    assert engine_ps.metrics["pts_per100"] == EXPECTED_PTS_PER100

    await rebuild(db_session)
    await db_session.commit()
    stored = (
        await db_session.execute(
            select(SummerLeaguePlayerSeason).where(
                SummerLeaguePlayerSeason.competition_id == comp_id,
                SummerLeaguePlayerSeason.player_id == target.id,
                SummerLeaguePlayerSeason.is_current.is_(True),  # type: ignore[attr-defined]
            )
        )
    ).scalar_one()
    assert stored.adv_eligible is True
    assert stored.per == EXPECTED_PER
    assert stored.ws == EXPECTED_WS
    assert stored.pace == EXPECTED_PACE
    assert stored.pts_per100 == EXPECTED_PTS_PER100

    explorer_result = await run_explorer_query(
        db_session,
        ExplorerQuery(
            subject="players",
            grain="per_competition",
            year_min=_YEAR,
            year_max=_YEAR,
            venue=_VENUE,
            mode="per_game",
            player_slug=slug,
            min_games=1,
            min_minutes=1,
        ),
    )
    explorer_row = explorer_result.rows[0]
    assert explorer_row.values["per"] == EXPECTED_PER
    assert explorer_row.values["ws"] == EXPECTED_WS

    leaders_result = await get_leaders(
        db_session,
        mode="advanced",
        year=_YEAR,
        venue=_VENUE,
        min_games=1,
        min_minutes=1,
    )
    leader_row = next(r for r in leaders_result.rows if r.slug == slug)
    assert leader_row.values["per"] == EXPECTED_PER
    # FINDING (not a bug this ticket fixes -- see summer-league-phase2-stat-
    # engine-tickets.md T1 scope discipline): the leaderboard's advanced view
    # rounds WS to 1 decimal for display (`_round1` in `_leader_values`,
    # summer_league_metrics_service.py), while the stored column, the engine,
    # and the Explorer's per_competition cell all carry WS at 2 decimals. Same
    # underlying number, different display precision -- tolerate the rounding
    # here rather than pretend it's bit-identical.
    assert leader_row.values["ws"] == pytest.approx(round(EXPECTED_WS, 1), abs=0.05)


@pytest.mark.asyncio
async def test_per_36_and_totals_scaling_matches_across_explorer_and_leaderboard(
    db_session: AsyncSession,
) -> None:
    """Per-36 / totals scaled forms agree between the two surfaces that implement them.

    Game Score's per-mode scaling (``_compute_player_values`` in the Explorer
    service) and the counting-stat scaling in the leaderboard's
    ``_compute_row`` (``summer_league_leaders_service.py``) are two
    independently written implementations of the same "totals * factor"
    arithmetic -- exactly the duplication doc #1 item 1.3 / T4 consolidates.
    This pins today's agreement on ``pts`` (present on both surfaces) and on
    Game Score's totals/per_36 scaling (Explorer only -- the leaderboard's
    counting modes do not expose ``gmsc``, only the ``advanced`` mode does,
    and that mode is not scaled by games/minutes at all. This is a genuine
    surface gap, not a bug in this test: there is currently no leaderboard
    view of Game Score per-36. Flagged for T4/T6 rather than worked around.)

    Per-100 is included here because the leaderboard now computes its
    denominator from the same-grain engine possession estimate as the other
    surfaces. The fixture deliberately leaves the NBA game-log ``pace`` field
    unset, so this assertion protects the source reconciliation itself.
    """
    comp_id, slug = await _seed_fixture(db_session)
    await db_session.commit()
    async with db_session.begin():
        await rebuild(db_session)

    explorer_totals = await run_explorer_query(
        db_session,
        ExplorerQuery(
            subject="players",
            grain="per_competition",
            year_min=_YEAR,
            year_max=_YEAR,
            venue=_VENUE,
            mode="totals",
            player_slug=slug,
            min_games=1,
            min_minutes=1,
        ),
    )
    row = explorer_totals.rows[0]
    assert row.values["gmsc"] == EXPECTED_GMSC_TOTALS
    assert row.values["pts"] == 48

    explorer_per36 = await run_explorer_query(
        db_session,
        ExplorerQuery(
            subject="players",
            grain="per_competition",
            year_min=_YEAR,
            year_max=_YEAR,
            venue=_VENUE,
            mode="per_36",
            player_slug=slug,
            min_games=1,
            min_minutes=1,
        ),
    )
    explorer_row_36 = explorer_per36.rows[0]
    assert explorer_row_36.values["gmsc"] == EXPECTED_GMSC_PER_36
    assert explorer_row_36.values["pts"] == EXPECTED_PTS_PER_36

    explorer_per100 = await run_explorer_query(
        db_session,
        ExplorerQuery(
            subject="players",
            grain="per_competition",
            year_min=_YEAR,
            year_max=_YEAR,
            venue=_VENUE,
            mode="per_100",
            player_slug=slug,
            min_games=1,
            min_minutes=1,
        ),
    )
    explorer_row_100 = explorer_per100.rows[0]
    assert explorer_row_100.values["pts"] == EXPECTED_PTS_PER100

    leaders_totals = await get_leaders(
        db_session,
        mode="totals",
        year=_YEAR,
        venue=_VENUE,
        min_games=1,
        min_minutes=1,
    )
    leader_row_totals = next(r for r in leaders_totals.rows if r.slug == slug)
    assert leader_row_totals.values["pts"] == 48

    leaders_per36 = await get_leaders(
        db_session,
        mode="per_36",
        year=_YEAR,
        venue=_VENUE,
        min_games=1,
        min_minutes=1,
    )
    leader_row_36 = next(r for r in leaders_per36.rows if r.slug == slug)
    assert leader_row_36.values["pts"] == EXPECTED_PTS_PER_36

    leaders_per100 = await get_leaders(
        db_session,
        mode="per_100",
        year=_YEAR,
        venue=_VENUE,
        sort="pts",
        min_games=1,
        min_minutes=1,
    )
    leader_row_100 = next(r for r in leaders_per100.rows if r.slug == slug)
    assert leader_row_100.values["pts"] == EXPECTED_PTS_PER100
