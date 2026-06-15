"""Unit tests for the Summer League advanced-metrics math.

Covers the pure computation: Game Score, the safe-divide guard, the SL
Pythagorean fit, the BPM offense/defense split invariant, and the advanced-metric
gating that blanks league-relative stats for ineligible pools.
"""

from __future__ import annotations

from app.services.summer_league.metrics import (
    BPM_FEATURES,
    Box,
    LeagueContext,
    PlayerSeason,
    _d,
    apply_sl_bpm,
    compute_metrics,
    fit_pythagorean,
    game_score,
)


def _box(**kw: float) -> Box:
    b = Box()
    for k, v in kw.items():
        setattr(b, k, v)
    return b


def test_safe_divide_guards_zero() -> None:
    """``_d`` returns 0.0 rather than raising on a zero denominator."""
    assert _d(10.0, 2.0) == 5.0
    assert _d(10.0, 0.0) == 0.0


def test_game_score_matches_hollinger_formula() -> None:
    """Game Score equals the Hollinger weighting of a known box line."""
    b = _box(
        pts=20, fgm=8, fga=15, fta=4, ftm=3, oreb=2, dreb=5,
        stl=2, ast=4, blk=1, pf=3, tov=2,
    )
    # 20 +0.4*8 -0.7*15 -0.4*(4-3) +0.7*2 +0.3*5 +2 +0.7*4 +0.7*1 -0.4*3 -2
    expected = (
        20 + 0.4 * 8 - 0.7 * 15 - 0.4 * 1 + 0.7 * 2 + 0.3 * 5
        + 2 + 0.7 * 4 + 0.7 * 1 - 0.4 * 3 - 2
    )
    assert game_score(b) == round(expected, 6)


def test_fit_pythagorean_recovers_known_exponent() -> None:
    """With W/L = (PF/PA)^2, the fit returns x ≈ 2.0."""
    # ln(36/25)/ln(120/100) == 2.0 exactly.
    records = {i: {"w": 36, "l": 25, "pf": 120, "pa": 100} for i in range(25)}
    team_comp = {i: 1 for i in range(25)}
    x, n = fit_pythagorean(records, team_comp, {1})
    assert n == 25
    assert abs(x - 2.0) < 1e-6


def test_fit_pythagorean_falls_back_when_thin() -> None:
    """Too few decided records → the NBA-ish fallback exponent."""
    records = {0: {"w": 3, "l": 1, "pf": 110, "pa": 100}}
    x, n = fit_pythagorean(records, {0: 1}, {1})
    assert x == 13.0
    assert n == 1


def _ps_with_poss(pm: float, mp: float, poss: float, **box: float) -> PlayerSeason:
    ps = PlayerSeason(
        player_id=1, competition_id=1, primary_team_entry_id=1,
        year=2025, venue="las_vegas",
        box=_box(mp=mp, **box), team=Box(), opp=Box(), pm=pm,
    )
    ps.player_poss = poss
    ps.pct_min = mp / 40.0
    return ps


def test_bpm_split_reconstructs_bpm_and_centers_to_zero() -> None:
    """OBPM + DBPM == BPM, and the minute-weighted pool mean BPM is ~0."""
    coef = {f: 1.0 for f in BPM_FEATURES}
    pool = [
        _ps_with_poss(10, 100, 100, fgm=8, fg3m=2, ftm=4, fga=14, fta=5,
                      oreb=2, dreb=6, ast=5, stl=2, blk=1, tov=2, pf=3),
        _ps_with_poss(-6, 80, 90, fgm=3, fg3m=1, ftm=2, fga=10, fta=3,
                      oreb=1, dreb=3, ast=2, stl=1, blk=0, tov=4, pf=4),
        _ps_with_poss(2, 120, 110, fgm=5, fg3m=2, ftm=3, fga=12, fta=4,
                      oreb=2, dreb=5, ast=4, stl=1, blk=1, tov=3, pf=2),
    ]
    by_pool = {1: pool}
    apply_sl_bpm([p for p in pool], by_pool, coef, intercept=-5.0)

    for ps in pool:
        bpm, obpm, dbpm = ps.metrics["bpm"], ps.metrics["obpm"], ps.metrics["dbpm"]
        assert bpm is not None and obpm is not None and dbpm is not None
        assert bpm == round(obpm + dbpm, 1)
    wmp = sum(p.box.mp for p in pool)
    mean_bpm = sum((p.metrics["bpm"] or 0.0) * p.box.mp for p in pool) / wmp
    assert abs(mean_bpm) < 0.06  # within rounding of zero


def test_compute_metrics_blanks_composites_when_pool_ineligible() -> None:
    """Ineligible pools keep box/shooting but null league-relative composites."""
    ctx = LeagueContext(
        competition_id=1, year=2025, venue="las_vegas",
        lg=Box(), poss=0.0, team_games=0, adv_eligible=False,
    )
    ps = PlayerSeason(
        player_id=1, competition_id=1, primary_team_entry_id=1,
        year=2025, venue="las_vegas",
        box=_box(mp=100, pts=20, fgm=8, fga=15, fta=4, ftm=3, fg3m=2, fg3a=5,
                 gp=4),
        team=Box(), opp=Box(),
    )
    compute_metrics(ps, ctx, ws_ppw_coeff=0.43)
    # Shooting/box still computed.
    assert ps.metrics["gmsc"] is not None
    assert ps.metrics["ts_pct"] is not None
    # League-relative composites blanked.
    assert ps.metrics["per"] is None
    assert ps.metrics["ortg"] is None
    assert ps.metrics["ws"] is None
