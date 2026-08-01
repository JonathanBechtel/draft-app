"""Unit tests for the shared percentile primitive (#723).

Covers the two functions in :mod:`app.services.stats.percentiles` directly:
single-element input, exact-rank input (no interpolation), interpolated-rank
input, empty input (each function's own contract -- both raise), and
forward/reverse round-trip consistency. These are the unrounded primitives;
rounding and empty-input handling are exercised at each call site's own test
module (``test_summer_league_environment_service.py``,
``test_sl_cohort_baselines.py``, ``test_sl_desk_grades.py``), which this
change leaves passing unchanged.
"""

from __future__ import annotations

import pytest

from app.services.stats.percentiles import percentile, percentile_of

# --------------------------------------------------------------------------- #
# percentile (forward)
# --------------------------------------------------------------------------- #


def test_percentile_single_element_returns_it_regardless_of_q() -> None:
    """A one-element distribution has no spread; every quantile is that value."""
    assert percentile([5.0], 0.0) == 5.0
    assert percentile([5.0], 0.3) == 5.0
    assert percentile([5.0], 1.0) == 5.0


def test_percentile_exact_rank_no_interpolation() -> None:
    """q landing exactly on an index needs no interpolation between neighbors."""
    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert percentile(values, 0.5) == pytest.approx(30.0)
    assert percentile(values, 0.0) == pytest.approx(10.0)
    assert percentile(values, 1.0) == pytest.approx(50.0)


def test_percentile_interpolated_rank() -> None:
    """q landing between two ranks linearly interpolates (numpy 'linear' method)."""
    values = [10.0, 20.0, 30.0, 40.0]
    assert percentile(values, 0.25) == pytest.approx(17.5)
    assert percentile(values, 0.75) == pytest.approx(32.5)


def test_percentile_sorts_unordered_input() -> None:
    """Input order doesn't matter; the function sorts internally."""
    assert percentile([40.0, 10.0, 30.0, 20.0], 0.5) == pytest.approx(25.0)


def test_percentile_empty_raises() -> None:
    """Matches the environment-service ``_percentile`` contract: raise, don't return None."""
    with pytest.raises(ValueError):
        percentile([], 0.5)


# --------------------------------------------------------------------------- #
# percentile_of (reverse)
# --------------------------------------------------------------------------- #


def test_percentile_of_exact_grid_point() -> None:
    """A value exactly at a fitted breakpoint returns exactly that percentile."""
    breakpoints = {"0": 10.0, "50": 30.0, "100": 50.0}
    assert percentile_of(breakpoints, 30.0) == pytest.approx(50.0)


def test_percentile_of_interpolated_rank() -> None:
    """A value strictly between two grid points interpolates linearly."""
    breakpoints = {"0": 10.0, "50": 30.0, "100": 50.0}
    assert percentile_of(breakpoints, 20.0) == pytest.approx(25.0)


def test_percentile_of_single_entry_clamps_both_directions() -> None:
    """A one-point grid has no interior; any value clamps to that single percentile."""
    breakpoints = {"50": 42.0}
    assert percentile_of(breakpoints, 10.0) == pytest.approx(50.0)
    assert percentile_of(breakpoints, 42.0) == pytest.approx(50.0)
    assert percentile_of(breakpoints, 100.0) == pytest.approx(50.0)


def test_percentile_of_below_and_above_range_clamp() -> None:
    """Values outside the observed grid clamp to the nearest end, not extrapolate."""
    breakpoints = {"0": 10.0, "50": 30.0, "100": 50.0}
    assert percentile_of(breakpoints, 0.0) == pytest.approx(0.0)
    assert percentile_of(breakpoints, 999.0) == pytest.approx(100.0)


def test_percentile_of_empty_raises() -> None:
    """Matches the desk_grades ``percentile_of_value`` contract: raise on an empty cohort."""
    with pytest.raises(ValueError):
        percentile_of({}, 25.0)


# --------------------------------------------------------------------------- #
# forward / reverse round-trip
# --------------------------------------------------------------------------- #


def test_forward_reverse_round_trip() -> None:
    """Fitting a grid with ``percentile`` and inverting it with ``percentile_of``
    recovers the original percentile at each fitted value."""
    values = [12.0, 45.0, 7.0, 89.0, 33.0, 21.0, 60.0, 5.0]
    fitted_percentiles = (0, 25, 50, 75, 100)
    breakpoints = {str(p): percentile(values, p / 100.0) for p in fitted_percentiles}
    for p in fitted_percentiles:
        assert percentile_of(breakpoints, breakpoints[str(p)]) == pytest.approx(float(p))
