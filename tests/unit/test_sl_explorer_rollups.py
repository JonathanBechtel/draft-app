"""Unit tests for the Summer League Explorer roll-up primitives (ticket #404).

Covers all three roll-up functions:

* ``rollup_additive``      — sum across pools, skip None, all-None → None
* ``rollup_rate_composite`` — minute-weighted avg, skip null/zero-minute pools
* ``rollup_recombinable``  — recompute ratio from summed box components (NOT a mean
                             of per-competition values)

Each function accepts a sequence of objects (duck-typed; rows are simple dataclasses
here rather than real ORM rows — the functions only call ``getattr``).
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Optional

import pytest

from app.services.sources.summer_league.constants import MINUTES_PER_GAME
from app.services.summer_league_explorer_service import (
    _compute_player_values,
    rollup_additive,
    rollup_rate_composite,
    rollup_recombinable,
)


# --------------------------------------------------------------------------- #
# Test row fixture
# --------------------------------------------------------------------------- #


@dataclass
class _Row:
    """Minimal per-competition row for unit testing roll-ups."""

    # Minutes played (used for minute-weighting in rollup_rate_composite).
    minutes: float = 0.0
    # Box totals (used by rollup_recombinable).
    pts: int = 0
    fgm: int = 0
    fga: int = 0
    fg3m: int = 0
    fg3a: int = 0
    ftm: int = 0
    fta: int = 0
    # Advanced composites (used by rollup_rate_composite / rollup_additive).
    per: Optional[float] = None
    bpm: Optional[float] = None
    ws: Optional[float] = None
    vorp: Optional[float] = None
    ows: Optional[float] = None
    dws: Optional[float] = None
    gmsc: Optional[float] = None
    ws82: Optional[float] = None
    vorp82: Optional[float] = None
    ts_pct: Optional[float] = None
    efg_pct: Optional[float] = None
    fg3ar: Optional[float] = None
    ftr: Optional[float] = None
    pts_per100: Optional[float] = None
    usg_pct: Optional[float] = None
    pace: Optional[float] = None


# --------------------------------------------------------------------------- #
# rollup_additive
# --------------------------------------------------------------------------- #


def test_additive_happy_path_sums_values() -> None:
    """rollup_additive sums non-None values across rows."""
    rows = [_Row(ws=1.2), _Row(ws=0.8), _Row(ws=2.0)]
    result = rollup_additive(rows, "ws")
    assert result == pytest.approx(4.0, abs=1e-9)


def test_additive_skips_none_values() -> None:
    """None values are skipped; only non-None values contribute to the sum."""
    rows = [_Row(ws=1.5), _Row(ws=None), _Row(ws=2.5)]
    result = rollup_additive(rows, "ws")
    assert result == pytest.approx(4.0, abs=1e-9)


def test_additive_all_none_returns_none() -> None:
    """When every row has None, rollup_additive returns None (no data, not zero)."""
    rows = [_Row(ws=None), _Row(ws=None)]
    assert rollup_additive(rows, "ws") is None


def test_additive_empty_rows_returns_none() -> None:
    """An empty sequence returns None."""
    assert rollup_additive([], "ws") is None


def test_additive_zeros_are_counted_not_skipped() -> None:
    """Zero values are real data — they are NOT skipped, only None is skipped."""
    rows = [_Row(ws=0.0), _Row(ws=None), _Row(ws=3.0)]
    result = rollup_additive(rows, "ws")
    assert result == pytest.approx(3.0, abs=1e-9)


def test_additive_single_value() -> None:
    """A single-row sequence returns that row's value."""
    assert rollup_additive([_Row(vorp=0.4)], "vorp") == pytest.approx(0.4, abs=1e-9)


def test_additive_negative_values_sum_correctly() -> None:
    """Negative values (e.g. negative VORP) are summed algebraically."""
    rows = [_Row(vorp=-0.3), _Row(vorp=1.2)]
    assert rollup_additive(rows, "vorp") == pytest.approx(0.9, abs=1e-9)


# --------------------------------------------------------------------------- #
# rollup_rate_composite
# --------------------------------------------------------------------------- #


def test_rate_composite_happy_path_weighted_average() -> None:
    """rollup_rate_composite returns the minute-weighted mean."""
    # Pool A: PER 20, 100 min; Pool B: PER 10, 200 min
    # Weighted mean = (20*100 + 10*200) / (100+200) = 4000/300 ≈ 13.33
    rows = [_Row(per=20.0, minutes=100.0), _Row(per=10.0, minutes=200.0)]
    result = rollup_rate_composite(rows, "per")
    assert result == pytest.approx(40.0 / 3.0, abs=1e-6)


def test_rate_composite_skips_none_value_pools() -> None:
    """Pools with a None composite value are excluded from numerator and denominator."""
    rows = [
        _Row(per=15.0, minutes=100.0),
        _Row(per=None, minutes=200.0),  # ineligible pool — not adv-eligible
        _Row(per=20.0, minutes=100.0),
    ]
    # Only first and third pools contribute: (15*100 + 20*100) / 200 = 17.5
    result = rollup_rate_composite(rows, "per")
    assert result == pytest.approx(17.5, abs=1e-6)


def test_rate_composite_skips_zero_minute_pools() -> None:
    """Pools with zero minutes are excluded (would produce infinite rate)."""
    rows = [
        _Row(per=15.0, minutes=100.0),
        _Row(per=25.0, minutes=0.0),  # zero minutes — skip
    ]
    result = rollup_rate_composite(rows, "per")
    assert result == pytest.approx(15.0, abs=1e-6)


def test_rate_composite_all_none_returns_none() -> None:
    """Returns None when no eligible pool exists."""
    rows = [_Row(per=None, minutes=100.0), _Row(per=None, minutes=200.0)]
    assert rollup_rate_composite(rows, "per") is None


def test_rate_composite_all_zero_minutes_returns_none() -> None:
    """Returns None when every row has zero minutes (no weight)."""
    rows = [_Row(per=15.0, minutes=0.0), _Row(per=20.0, minutes=0.0)]
    assert rollup_rate_composite(rows, "per") is None


def test_rate_composite_empty_rows_returns_none() -> None:
    """An empty sequence returns None."""
    assert rollup_rate_composite([], "bpm") is None


def test_rate_composite_minute_weighting_is_correct() -> None:
    """The higher-minute pool has more influence on the weighted mean.

    This distinguishes a true weighted average from a simple mean.
    """
    # 90 min at BPM +10; 10 min at BPM -10 → should be clearly positive
    rows = [_Row(bpm=10.0, minutes=90.0), _Row(bpm=-10.0, minutes=10.0)]
    result = rollup_rate_composite(rows, "bpm")
    # (10*90 + (-10)*10) / 100 = (900 - 100) / 100 = 8.0
    assert result == pytest.approx(8.0, abs=1e-6)
    # Plain average would give 0.0; verify we're NOT returning the simple mean
    assert result != pytest.approx(0.0, abs=1e-3)


def test_rate_composite_percentage_columns_weighted_correctly() -> None:
    """Rate composites stored as percentages (e.g. usg_pct=25.6) weight by minutes."""
    rows = [
        _Row(usg_pct=30.0, minutes=120.0),
        _Row(usg_pct=20.0, minutes=80.0),
    ]
    # (30*120 + 20*80) / (120+80) = (3600+1600)/200 = 26.0
    result = rollup_rate_composite(rows, "usg_pct")
    assert result == pytest.approx(26.0, abs=1e-6)


# --------------------------------------------------------------------------- #
# rollup_recombinable
# --------------------------------------------------------------------------- #


def _box(
    fgm: int = 0,
    fga: int = 0,
    fg3m: int = 0,
    fg3a: int = 0,
    ftm: int = 0,
    fta: int = 0,
    pts: int = 0,
    minutes: float = 10.0,
    pace: Optional[float] = None,
) -> _Row:
    return _Row(
        fgm=fgm,
        fga=fga,
        fg3m=fg3m,
        fg3a=fg3a,
        ftm=ftm,
        fta=fta,
        pts=pts,
        minutes=minutes,
        pace=pace,
    )


def test_recombinable_ts_pct_from_summed_components() -> None:
    """TS% = PTS / (2 * (FGA + 0.44*FTA)) * 100, computed from summed totals."""
    # Pool A: 10 pts, 5 fga, 2 fta → ts_denom = 2*(5+0.88)=11.76, ts=85.03...
    # Pool B: 8 pts, 4 fga, 4 fta  → ts_denom = 2*(4+1.76)=11.52, ts=69.44...
    # Summed: 18 pts, 9 fga, 6 fta → ts_denom=2*(9+2.64)=23.28, ts=18/23.28*100≈77.32
    rows = [
        _box(pts=10, fga=5, fta=2),
        _box(pts=8, fga=4, fta=4),
    ]
    result = rollup_recombinable(rows, "ts_pct")
    expected = 100.0 * 18 / (2.0 * (9 + 0.44 * 6))
    assert result == pytest.approx(expected, abs=1e-6)


def test_recombinable_ts_pct_not_mean_of_per_competition_values() -> None:
    """Recombinable TS% must NOT be a simple mean of per-competition TS% values.

    Averaging per-competition ratios over-weights small pools.  Verify that the
    function recomputes from box totals and the result differs from the naive mean.
    """
    rows = [
        _box(pts=100, fga=50, fta=20),  # TS% ≈ 86.2
        _box(pts=2, fga=2, fta=0),  # TS% = 50.0  (tiny pool)
    ]
    naive_per_comp_ts = [
        100.0 * 100 / (2.0 * (50 + 0.44 * 20)),
        100.0 * 2 / (2.0 * (2 + 0.44 * 0)),
    ]
    naive_mean = sum(naive_per_comp_ts) / 2

    result = rollup_recombinable(rows, "ts_pct")
    assert result is not None
    # The recomputed value weights by volume; naive mean gives equal weight to the
    # tiny pool.  They must differ.
    assert result != pytest.approx(naive_mean, abs=0.01)

    # And the recomputed result is closer to the large-pool TS% (~86.2), not the
    # midpoint of per-comp values.
    assert result > naive_mean, "Recomputed TS% should be pulled toward the larger pool"


def test_recombinable_efg_pct() -> None:
    """eFG% = (FGM + 0.5*FG3M) / FGA * 100, computed from summed totals."""
    rows = [
        _box(fgm=4, fga=10, fg3m=2),  # eFG% = (4+1)/10 = 50
        _box(fgm=3, fga=8, fg3m=1),  # eFG% = (3+0.5)/8 = 43.75
    ]
    result = rollup_recombinable(rows, "efg_pct")
    # Summed: fgm=7, fga=18, fg3m=3 → (7+1.5)/18*100 = 8.5/18*100 ≈ 47.22
    expected = 100.0 * (7 + 0.5 * 3) / 18
    assert result == pytest.approx(expected, abs=1e-6)


def test_recombinable_fg_pct() -> None:
    """FG% = FGM / FGA * 100."""
    rows = [_box(fgm=3, fga=10), _box(fgm=5, fga=10)]
    result = rollup_recombinable(rows, "fg_pct")
    assert result == pytest.approx(100.0 * 8 / 20, abs=1e-6)


def test_recombinable_fg3_pct() -> None:
    """3P% = FG3M / FG3A * 100."""
    rows = [_box(fg3m=2, fg3a=6), _box(fg3m=3, fg3a=9)]
    result = rollup_recombinable(rows, "fg3_pct")
    assert result == pytest.approx(100.0 * 5 / 15, abs=1e-6)


def test_recombinable_ft_pct() -> None:
    """FT% = FTM / FTA * 100."""
    rows = [_box(ftm=4, fta=5), _box(ftm=6, fta=10)]
    result = rollup_recombinable(rows, "ft_pct")
    assert result == pytest.approx(100.0 * 10 / 15, abs=1e-6)


def test_recombinable_fg3ar() -> None:
    """3PAr = FG3A / FGA as a 0-1 fraction (BBRef scale, matches stored column)."""
    rows = [_box(fga=10, fg3a=4), _box(fga=8, fg3a=2)]
    result = rollup_recombinable(rows, "fg3ar")
    assert result == pytest.approx(6 / 18, abs=1e-6)


def test_recombinable_ftr() -> None:
    """FTr = FTA / FGA as a 0-1 fraction (BBRef scale, matches stored column)."""
    rows = [_box(fga=10, fta=5), _box(fga=8, fta=4)]
    result = rollup_recombinable(rows, "ftr")
    assert result == pytest.approx(9 / 18, abs=1e-6)


def test_recombinable_pts_per100() -> None:
    """pts_per100 = 100 * PTS / (sum(pace*minutes)/48).

    ``pace`` is possessions per 48 minutes (NBA's normalization base, kept even
    for 40-minute Summer League games), so possessions divide by MINUTES_PER_GAME.
    """
    # pace=96 poss/48min, 48 min → 96 poss; pts=24 → 25 pts/100 poss
    rows = [_box(pts=24, minutes=48.0, pace=96.0)]
    result = rollup_recombinable(rows, "pts_per100")
    # poss = 96*48/48 = 96; pts_per100 = 24*100/96 = 25.0
    assert result == pytest.approx(25.0, abs=1e-6)


def test_recombinable_pts_per100_pools_combined() -> None:
    """pts_per100 over two pools sums pts and per-48 possession estimates."""
    rows = [
        _box(pts=20, minutes=40.0, pace=80.0),
        _box(pts=15, minutes=30.0, pace=100.0),
    ]
    # Total pts=35, total poss = (80*40 + 100*30)/48 = (3200+3000)/48 ≈ 129.17
    result = rollup_recombinable(rows, "pts_per100")
    expected = 100.0 * 35 / ((80 * 40 + 100 * 30) / MINUTES_PER_GAME)
    assert result == pytest.approx(expected, abs=1e-6)


def test_all_services_share_possession_base() -> None:
    """Possessions divide by one shared per-48 base, so the per-100 math can't drift.

    The pooled Explorer rollup and Summer League stats service use the single
    ``sources.summer_league.constants.MINUTES_PER_GAME``. Display-only per-mode scaling
    delegates to ``app.services.stats.scaling``, whose SQL/Python pair keeps
    leaderboards and rendered values in lockstep.

    The engine cannot import the Summer League constant — import-linter
    contract 3 forbids ``app.services.stats -> app.services.sources.summer_league*`` —
    so the 48 is baked into ``_PER_100_NUMERATOR`` (``100 * 60 * 48``). That is
    the drift this asserts against: the one place the two packages must agree
    numerically without being able to share a symbol.
    """
    from app.services import summer_league_stats_service as sts
    from app.services.stats import scaling

    assert MINUTES_PER_GAME == 48.0
    assert sts._MINUTES_PER_GAME is MINUTES_PER_GAME
    assert scaling._PER_100_NUMERATOR == 100.0 * 60.0 * MINUTES_PER_GAME


def test_recombinable_pts_per100_extrapolates_partial_pace() -> None:
    """A competition missing pace has its possessions extrapolated from covered ones.

    Covered comp: 30 min at pace 100 → 62.5 poss. Uncovered comp: 30 min, 90 pts.
    Extrapolating to all 60 min → 125 poss; 120 pts / 125 poss = 96.0 per-100.
    (Subset-only would give 48.0; the old full/partial-denominator bug gave 192.0.)
    """
    rows = [
        _box(pts=30, minutes=30.0, pace=100.0),
        _box(pts=90, minutes=30.0, pace=None),
    ]
    result = rollup_recombinable(rows, "pts_per100")
    assert result == pytest.approx(100.0 * 120 / 125, abs=1e-6)


def test_recombinable_pts_per100_all_missing_pace_returns_none() -> None:
    """With no pace-covered minutes, possessions are unknown → None (not a guess)."""
    rows = [
        _box(pts=30, minutes=30.0, pace=None),
        _box(pts=10, minutes=10.0, pace=None),
    ]
    assert rollup_recombinable(rows, "pts_per100") is None


def _agg_row(*, pts: int, sec: float, pace_sec: float, gp: int = 1) -> SimpleNamespace:
    """Aggregated player row shaped for :func:`_compute_player_values`.

    ``sec`` is seconds played; ``pace_sec`` is the pace-weighted seconds
    (``SUM(pace * minutes_seconds)`` in SQL). Every counting field the mode
    scaler reads must exist, so default the box components to zero.
    """
    fields: dict[str, float] = {
        c: 0.0
        for c in (
            "pts",
            "reb",
            "ast",
            "stl",
            "blk",
            "tov",
            "oreb",
            "dreb",
            "pf",
            "fgm",
            "fga",
            "fg3m",
            "fg3a",
            "ftm",
            "fta",
        )
    }
    fields.update(pts=pts, gp=gp, sec=sec, pace_sec=pace_sec, plus_minus=0.0)
    return SimpleNamespace(**fields)


def test_pts_per100_reconciles_recombinable_and_mode_path() -> None:
    """The pooled Pts/100 column and the per-100 *mode* PTS cell must agree.

    Both recover possessions from the same per-48 pace base (MINUTES_PER_GAME);
    if either path drifts (e.g. back to a /40 divisor) this equality breaks. A
    /40-vs-/48 mismatch would differ by ~20%, far beyond the rounding tolerance.
    """
    pts, pace, minutes = 20, 95.0, 120.0
    recombinable = rollup_recombinable(
        [_box(pts=pts, minutes=minutes, pace=pace)], "pts_per100"
    )

    # Mode-scaling path: seconds = minutes*60; pace_sec = pace * minutes_seconds.
    agg = _agg_row(pts=pts, sec=minutes * 60.0, pace_sec=pace * minutes * 60.0)
    mode_pts = _compute_player_values(agg, "per_100")["pts"]

    assert recombinable is not None and mode_pts is not None
    # mode_pts is rounded to 1 decimal; 0.05 absorbs rounding but not a base mismatch.
    assert recombinable == pytest.approx(mode_pts, abs=0.05)
    expected = 100.0 * pts / (pace * minutes / MINUTES_PER_GAME)
    assert recombinable == pytest.approx(expected, abs=1e-6)


def test_recombinable_zero_denominator_returns_none() -> None:
    """Returns None when the denominator is zero (no attempts)."""
    rows = [_box(fga=0, fta=0, pts=0)]
    assert rollup_recombinable(rows, "ts_pct") is None
    assert rollup_recombinable(rows, "efg_pct") is None
    assert rollup_recombinable(rows, "fg_pct") is None
    assert rollup_recombinable(rows, "fg3_pct") is None
    assert rollup_recombinable(rows, "ft_pct") is None
    assert rollup_recombinable(rows, "fg3ar") is None
    assert rollup_recombinable(rows, "ftr") is None


def test_recombinable_empty_rows_returns_none() -> None:
    """An empty sequence always returns None."""
    for key in ("ts_pct", "efg_pct", "fg_pct", "fg3_pct", "ft_pct", "fg3ar", "ftr"):
        assert rollup_recombinable([], key) is None, f"Expected None for key={key!r}"


def test_recombinable_unknown_key_returns_none() -> None:
    """An unrecognised key (not a recombinable metric) returns None gracefully."""
    rows = [_box(pts=10, fga=5, fta=2)]
    assert rollup_recombinable(rows, "per") is None
    assert rollup_recombinable(rows, "unknown_metric") is None


def test_recombinable_single_pool_matches_direct_formula() -> None:
    """For a single pool, rollup_recombinable must equal the direct formula result."""
    row = _box(pts=15, fgm=6, fga=12, fg3m=2, fg3a=5, ftm=3, fta=4)
    rows = [row]

    ts_direct = 100.0 * 15 / (2.0 * (12 + 0.44 * 4))
    efg_direct = 100.0 * (6 + 0.5 * 2) / 12
    fg_direct = 100.0 * 6 / 12
    fg3_direct = 100.0 * 2 / 5
    ft_direct = 100.0 * 3 / 4

    assert rollup_recombinable(rows, "ts_pct") == pytest.approx(ts_direct, abs=1e-6)
    assert rollup_recombinable(rows, "efg_pct") == pytest.approx(efg_direct, abs=1e-6)
    assert rollup_recombinable(rows, "fg_pct") == pytest.approx(fg_direct, abs=1e-6)
    assert rollup_recombinable(rows, "fg3_pct") == pytest.approx(fg3_direct, abs=1e-6)
    assert rollup_recombinable(rows, "ft_pct") == pytest.approx(ft_direct, abs=1e-6)


def test_recombinable_astd_pct_pools_pbp_counts() -> None:
    """AST'd% = 100 · Σast_fgm / Σ(ast_fgm + unast_fgm); None with no PBP counts."""
    from types import SimpleNamespace

    rows = [
        SimpleNamespace(ast_fgm=6, unast_fgm=4),
        SimpleNamespace(ast_fgm=2, unast_fgm=8),
    ]
    assert rollup_recombinable(rows, "astd_pct") == pytest.approx(40.0)
    # Pre-PBP rows carry None counts → no denominator → None, not 0.
    no_pbp = [SimpleNamespace(ast_fgm=None, unast_fgm=None)]
    assert rollup_recombinable(no_pbp, "astd_pct") is None
