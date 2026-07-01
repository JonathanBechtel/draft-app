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
    game_score_from_row,
    game_score_line,
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
        pts=20,
        fgm=8,
        fga=15,
        fta=4,
        ftm=3,
        oreb=2,
        dreb=5,
        stl=2,
        ast=4,
        blk=1,
        pf=3,
        tov=2,
    )
    # 20 +0.4*8 -0.7*15 -0.4*(4-3) +0.7*2 +0.3*5 +2 +0.7*4 +0.7*1 -0.4*3 -2
    expected = (
        20
        + 0.4 * 8
        - 0.7 * 15
        - 0.4 * 1
        + 0.7 * 2
        + 0.3 * 5
        + 2
        + 0.7 * 4
        + 0.7 * 1
        - 0.4 * 3
        - 2
    )
    assert game_score(b) == round(expected, 6)


def test_game_score_line_matches_box_and_coalesces_none() -> None:
    """game_score_line equals game_score(Box) and treats missing/None components as 0."""
    b = _box(
        pts=20,
        fgm=8,
        fga=15,
        fta=4,
        ftm=3,
        oreb=2,
        dreb=5,
        stl=2,
        ast=4,
        blk=1,
        pf=3,
        tov=2,
    )
    assert game_score_line(
        pts=20,
        fgm=8,
        fga=15,
        ftm=3,
        fta=4,
        oreb=2,
        dreb=5,
        ast=4,
        stl=2,
        blk=1,
        tov=2,
        pf=3,
    ) == game_score(b)
    # None components coalesce to 0 (here: only points score).
    assert (
        game_score_line(
            pts=10,
            fgm=None,
            fga=None,
            ftm=None,
            fta=None,
            oreb=None,
            dreb=None,
            ast=None,
            stl=None,
            blk=None,
            tov=None,
            pf=None,
        )
        == 10.0
    )


def test_game_score_from_row_handles_objects_and_mappings() -> None:
    """game_score_from_row reads box fields from either an object or a mapping.

    Both forms (an attribute-bearing object and a dict of summed totals) must agree
    with game_score_line, and absent fields coalesce to 0.
    """
    from types import SimpleNamespace

    fields = dict(
        pts=20, fgm=8, fga=15, ftm=3, fta=4, oreb=2, dreb=5,
        ast=4, stl=2, blk=1, tov=2, pf=3,
    )
    expected = game_score_line(**fields)
    assert game_score_from_row(SimpleNamespace(**fields)) == expected
    # Mapping path; extra keys (e.g. reb) are ignored, missing keys → 0.
    assert game_score_from_row({**fields, "reb": 7}) == expected
    assert game_score_from_row({"pts": 10}) == 10.0
    assert game_score_from_row(SimpleNamespace(pts=10)) == 10.0


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
        player_id=1,
        competition_id=1,
        primary_team_entry_id=1,
        year=2025,
        venue="las_vegas",
        box=_box(mp=mp, **box),
        team=Box(),
        opp=Box(),
        pm=pm,
    )
    ps.player_poss = poss
    ps.pct_min = mp / 40.0
    return ps


def test_bpm_split_reconstructs_bpm_and_centers_to_zero() -> None:
    """OBPM + DBPM == BPM, and the minute-weighted pool mean BPM is ~0."""
    coef = {f: 1.0 for f in BPM_FEATURES}
    pool = [
        _ps_with_poss(
            10,
            100,
            100,
            fgm=8,
            fg3m=2,
            ftm=4,
            fga=14,
            fta=5,
            oreb=2,
            dreb=6,
            ast=5,
            stl=2,
            blk=1,
            tov=2,
            pf=3,
        ),
        _ps_with_poss(
            -6,
            80,
            90,
            fgm=3,
            fg3m=1,
            ftm=2,
            fga=10,
            fta=3,
            oreb=1,
            dreb=3,
            ast=2,
            stl=1,
            blk=0,
            tov=4,
            pf=4,
        ),
        _ps_with_poss(
            2,
            120,
            110,
            fgm=5,
            fg3m=2,
            ftm=3,
            fga=12,
            fta=4,
            oreb=2,
            dreb=5,
            ast=4,
            stl=1,
            blk=1,
            tov=3,
            pf=2,
        ),
    ]
    by_pool = {1: pool}
    apply_sl_bpm([p for p in pool], by_pool, coef, intercept=-5.0)

    for ps in pool:
        bpm, obpm, dbpm = ps.metrics["bpm"], ps.metrics["obpm"], ps.metrics["dbpm"]
        assert bpm is not None and obpm is not None and dbpm is not None
        assert bpm == round(obpm + dbpm, 1)
        # Two value flavours: cumulative VORP on the standard MP/(48*82) yardstick
        # and an 82-game projection. The cumulative is never the larger magnitude
        # (a few games' worth of minutes is a fraction of a full season).
        vorp, vorp82 = ps.metrics["vorp"], ps.metrics["vorp82"]
        assert vorp is not None and vorp82 is not None
        assert abs(vorp) <= abs(vorp82)
    # At least one player has a non-trivial, clearly distinct pair of values.
    top = max(pool, key=lambda p: abs(p.metrics["vorp82"] or 0.0))
    top_vorp82, top_vorp = top.metrics["vorp82"], top.metrics["vorp"]
    assert top_vorp82 is not None and top_vorp is not None
    assert abs(top_vorp82) > abs(top_vorp) > 0.0
    wmp = sum(p.box.mp for p in pool)
    mean_bpm = sum((p.metrics["bpm"] or 0.0) * p.box.mp for p in pool) / wmp
    assert abs(mean_bpm) < 0.06  # within rounding of zero


def test_compute_metrics_blanks_composites_when_pool_ineligible() -> None:
    """Ineligible pools keep box/shooting but null league-relative composites."""
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
        primary_team_entry_id=1,
        year=2025,
        venue="las_vegas",
        box=_box(mp=100, pts=20, fgm=8, fga=15, fta=4, ftm=3, fg3m=2, fg3a=5, gp=4),
        team=_box(mp=1000, pts=400, fgm=150, fga=320, fta=90, oreb=40, tov=60),
        opp=_box(mp=1000, pts=390, fgm=148, fga=318, fta=88, oreb=38, tov=62),
    )
    compute_metrics(ps, ctx, ws_ppw_coeff=0.43)
    # Shooting/box still computed.
    assert ps.metrics["gmsc"] is not None
    assert ps.metrics["ts_pct"] is not None
    # pace / pts_per100 are raw possession measures — populated even when the pool
    # is ineligible, so per-100 works outside adv_eligible pools (issue #473).
    assert ps.metrics["pace"] is not None and ps.metrics["pace"] > 0
    assert ps.metrics["pts_per100"] is not None and ps.metrics["pts_per100"] > 0
    # League-relative / pool-calibrated composites blanked.
    assert ps.metrics["per"] is None
    assert ps.metrics["ortg"] is None
    assert ps.metrics["ws"] is None
    assert ps.metrics["ws82"] is None
