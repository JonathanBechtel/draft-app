"""Unit tests for the shared per-mode scaling definition (Phase 2, T4 / #725).

Covers :func:`app.services.stats.scaling.scale_python` and
:func:`app.services.stats.scaling.scale_sql` directly: each mode's factor,
zero/null denominators per mode, and the guarantee the two functions agree
(same numeric answer, Python vs. a hand-evaluated SQL fragment) -- the
property that lets a SQL ``ORDER BY`` rank rows on exactly what the Python
display path renders.
"""

from __future__ import annotations

import pytest

from app.services.stats.scaling import scale_python, scale_sql

# --------------------------------------------------------------------------- #
# scale_python -- per-mode factors
# --------------------------------------------------------------------------- #


def test_per_game_scales_by_inverse_gp() -> None:
    """value / gp, matching the box's per-game average."""
    result = scale_python(100.0, "per_game", gp=4, seconds=0.0, pace_seconds=0.0)
    assert result == pytest.approx(25.0)


def test_per_36_scales_by_2160_over_seconds() -> None:
    """36 min * 60 s / seconds played -- verified against a round-number fixture.

    120 total points over 40 minutes (2400 seconds) of play scales to a
    per-36 rate of 120 * 2160 / 2400 = 108.0.
    """
    result = scale_python(120.0, "per_36", gp=1, seconds=2400.0, pace_seconds=0.0)
    assert result == pytest.approx(108.0)


def test_per_100_scales_by_288000_over_pace_seconds() -> None:
    """100 poss * 60 s * 48 min / pace_seconds -- verified against a round fixture.

    pace_seconds = pace(96 poss/48min) * minutes(48) * 60 s = 276480; 24 points
    over that pace-weighted window scales to 24 * 288000 / 276480 = 25.0.
    """
    pace_seconds = 96.0 * 48.0 * 60.0
    result = scale_python(24.0, "per_100", gp=1, seconds=0.0, pace_seconds=pace_seconds)
    assert result == pytest.approx(25.0)


def test_totals_returns_value_unscaled() -> None:
    """totals mode never divides -- the value passes through untouched."""
    assert scale_python(77.0, "totals", gp=5, seconds=999.0, pace_seconds=999.0) == 77.0


# --------------------------------------------------------------------------- #
# scale_python -- zero/null denominators per mode
# --------------------------------------------------------------------------- #


def test_per_game_zero_gp_returns_none() -> None:
    assert scale_python(10.0, "per_game", gp=0, seconds=100.0, pace_seconds=100.0) is None


def test_per_36_zero_seconds_returns_none() -> None:
    assert scale_python(10.0, "per_36", gp=1, seconds=0.0, pace_seconds=100.0) is None


def test_per_100_zero_pace_seconds_returns_none() -> None:
    assert scale_python(10.0, "per_100", gp=1, seconds=100.0, pace_seconds=0.0) is None


def test_totals_ignores_zero_denominators() -> None:
    """totals mode has no denominator, so a zero gp/seconds/pace_seconds is irrelevant."""
    assert scale_python(42.0, "totals", gp=0, seconds=0.0, pace_seconds=0.0) == 42.0


# --------------------------------------------------------------------------- #
# scale_sql -- structural shape and NULLIF guards
# --------------------------------------------------------------------------- #


def test_scale_sql_per_game_forces_float_division() -> None:
    """The ``* 1.0`` is load-bearing -- Postgres integer division truncates ties."""
    expr = scale_sql("SUM(pts)", "COUNT(*)", "SUM(sec)", "SUM(pace_sec)", "per_game")
    assert expr == "SUM(pts) * 1.0 / NULLIF(COUNT(*), 0)"


def test_scale_sql_per_36_uses_2160_numerator() -> None:
    expr = scale_sql("SUM(pts)", "COUNT(*)", "SUM(sec)", "SUM(pace_sec)", "per_36")
    assert expr == "SUM(pts) * 2160.0 / NULLIF(SUM(sec), 0)"


def test_scale_sql_per_100_uses_288000_numerator() -> None:
    """288000 = 100 poss * 60 s * 48 min -- Summer League's per-48 pace base, not per-40."""
    expr = scale_sql("SUM(pts)", "COUNT(*)", "SUM(sec)", "SUM(pace_sec)", "per_100")
    assert expr == "SUM(pts) * 288000.0 / NULLIF(SUM(pace_sec), 0)"


def test_scale_sql_totals_passes_numerator_through_unguarded() -> None:
    expr = scale_sql("SUM(pts)", "COUNT(*)", "SUM(sec)", "SUM(pace_sec)", "totals")
    assert expr == "SUM(pts)"


# --------------------------------------------------------------------------- #
# scale_python / scale_sql agreement
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("mode", ["per_game", "per_36", "per_100", "totals"])
def test_python_and_sql_agree_on_a_shared_fixture(mode: str) -> None:
    """Evaluating the SQL fragment's arithmetic by hand must equal scale_python's answer.

    This is the property the SQL-sort COALESCE gotcha depends on: whatever
    scale_sql tells Postgres to compute in an ORDER BY must be the same number
    scale_python renders in the cell, for every mode.
    """
    value, gp, seconds, pace_seconds = 240.0, 6, 2880.0, 331776.0

    py_result = scale_python(value, mode, gp=gp, seconds=seconds, pace_seconds=pace_seconds)

    # Hand-evaluate the SQL fragment's arithmetic using the same numeric inputs
    # (NULLIF has no effect here since every denominator is non-zero).
    if mode == "per_game":
        sql_result: float = value * 1.0 / gp
    elif mode == "per_36":
        sql_result = value * 2160.0 / seconds
    elif mode == "per_100":
        sql_result = value * 288000.0 / pace_seconds
    else:
        sql_result = value

    assert py_result == pytest.approx(sql_result)
