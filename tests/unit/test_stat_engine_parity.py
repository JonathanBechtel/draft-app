"""Pure-engine leg of the golden-number parity harness (Phase 2, T1).

Exercises ``compute_metrics`` directly against a hand-built box line, no
database involved, and asserts its box-derived ("recombinable" — see
``docs/plans/summer-league-phase2-stat-engine-tickets.md`` T1/T7) outputs
against literal expected values worked out by hand from the formulas
documented in ``app/services/summer_league/metrics.py``.

This is one leg of the four-surface parity chain; the other three (stored
column, Explorer cell, leaderboard value) are checked against the *same*
fixture numbers in ``tests/integration/test_stat_engine_parity.py`` — see
that file's module docstring for the full picture and for why the expected
values are literals rather than something this test derives by calling the
engine.

Do not "fix" a failure here by recomputing the expected value from
``compute_metrics`` or ``game_score`` — a red result means the engine's
arithmetic changed, which is exactly what this harness exists to catch
before Phase 2's consolidation tickets (T4-T9) land.
"""

from __future__ import annotations

import pytest

from app.services.summer_league.metrics import (
    Box,
    LeagueContext,
    PlayerSeason,
    compute_metrics,
    game_score,
)

# One player's box line summed over 4 games at 30 minutes/game — the same
# per-game line and player used by the integration leg's DB fixture, so the
# literals recorded here and there are the same numbers by construction:
#   pts=12 fgm=5 fga=10 fg3m=1 fg3a=3 ftm=1 fta=2 oreb=1 dreb=3 reb=4 ast=2
#   stl=1 blk=1 tov=2 pf=2, 30 min, x4 games.
_PLAYER_BOX = Box(
    mp=120.0,
    gp=4,
    pts=48.0,
    fgm=20.0,
    fga=40.0,
    fg3m=4.0,
    fg3a=12.0,
    ftm=4.0,
    fta=8.0,
    oreb=4.0,
    dreb=12.0,
    reb=16.0,
    ast=8.0,
    stl=4.0,
    blk=4.0,
    tov=8.0,
    pf=8.0,
)


def _compute(box: Box) -> PlayerSeason:
    """Run ``compute_metrics`` on ``box`` with an ineligible (empty) pool context.

    Team/opponent boxes and the league context are left empty/ineligible: the
    box-derived ("recombinable") metrics under test here (TS%, eFG%, TOV%,
    3PAr, FTr, Game Score) only depend on the player's own box, so an empty
    team/opp is safe (the safe-divide helper ``_d`` returns 0.0, never raises)
    and ``adv_eligible=False`` short-circuits before the pool-recalibrated
    composites (PER, WS, ...) are touched — those are covered, with a real
    team/league context, by the integration leg instead.
    """
    ctx = LeagueContext(
        competition_id=1,
        year=2025,
        venue="las_vegas",
        lg=Box(),
        poss=0.0,
        team_games=0,
        adv_eligible=False,
    )
    ps = PlayerSeason(
        player_id=1,
        competition_id=1,
        primary_team_entry_id=None,
        year=2025,
        venue="las_vegas",
        box=box,
        team=Box(),
        opp=Box(),
    )
    compute_metrics(ps, ctx, ws_ppw_coeff=4.0 / 13.0)
    return ps


def test_game_score_raw_total_matches_hand_computed_literal() -> None:
    """Hollinger Game Score on the summed box totals.

    game_score = pts + 0.4*fgm - 0.7*fga - 0.4*(fta-ftm) + 0.7*oreb + 0.3*dreb
                 + stl + 0.7*ast + 0.7*blk - 0.4*pf - tov
               = 48 + 8 - 28 - 1.6 + 2.8 + 3.6 + 4 + 5.6 + 2.8 - 3.2 - 8
               = 34.0
    """
    assert game_score(_PLAYER_BOX) == pytest.approx(34.0)


def test_recombinable_metrics_match_hand_computed_literals() -> None:
    """TS%/eFG%/TOV%/3PAr/FTr/GmSc from a known box line match hand arithmetic.

    Box totals: pts=48 fgm=20 fga=40 fg3m=4 fg3a=12 ftm=4 fta=8, gp=4.

    TS%  = 100 * pts / (2 * (fga + 0.44*fta)) = 100*48 / (2*43.52) = 55.1
    eFG% = 100 * (fgm + 0.5*fg3m) / fga        = 100*22 / 40       = 55.0
    TOV% = 100 * tov / (fga + 0.44*fta + tov)  = 100*8 / 51.52     = 15.5
    3PAr = fg3a / fga                          = 12 / 40           = 0.300
    FTr  = fta / fga                           = 8 / 40            = 0.200
    GmSc (season, per-game avg) = game_score(box) / gp = 34.0 / 4  = 8.5
    """
    ps = _compute(_PLAYER_BOX)
    m = ps.metrics
    assert m["ts_pct"] == 55.1
    assert m["efg_pct"] == 55.0
    assert m["tov_pct"] == 15.5
    assert m["fg3ar"] == 0.3
    assert m["ftr"] == 0.2
    assert m["gmsc"] == 8.5


def test_ineligible_pool_leaves_pool_recalibrated_composites_null() -> None:
    """PER/ORtg/DRtg/WS are ``None`` when the pool context is not adv-eligible.

    Sanity check on the fixture helper: box-derived metrics above are computed
    unconditionally, but the pool-recalibrated ("league-relative") composites
    must not be silently populated from an empty/ineligible context.
    """
    ps = _compute(_PLAYER_BOX)
    for key in ("per", "ortg", "drtg", "net", "ows", "dws", "ws", "ws40", "ws82"):
        assert ps.metrics[key] is None
