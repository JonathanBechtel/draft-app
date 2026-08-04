"""Unit tests for the Summer League Desk cohort percentile + grade service (#503).

Pure-math and pure-classification coverage: percentile-of-value inversion,
grade thresholds, and the adaptive gate-ladder suppression decision. No DB —
see ``tests/integration/test_sl_desk_grades.py`` for the end-to-end grade +
T2 upsert.
"""

from __future__ import annotations

import pytest

from app.schemas.summer_league_desk import SummerLeagueDeskGrade
from app.services.sources.summer_league.desk_grades import (
    gate_rung,
    grade_for_percentile,
    is_gated,
    percentile_of_value,
)
from app.services.summer_league_leaders_service import GATE_LADDER, TARGET_BOARD_ROWS


# --------------------------------------------------------------------------- #
# percentile_of_value
# --------------------------------------------------------------------------- #
def test_percentile_of_value_exact_grid_point() -> None:
    """A value exactly at a fitted breakpoint returns exactly that percentile."""
    breakpoints = {"0": 10.0, "50": 30.0, "100": 50.0}
    assert percentile_of_value(breakpoints, 30.0) == 50.0


def test_percentile_of_value_interpolates_between_grid_points() -> None:
    """A value between two fitted points interpolates linearly."""
    breakpoints = {"0": 10.0, "50": 30.0, "100": 50.0}
    # Halfway between the 0th (10.0) and 50th (30.0) percentile points.
    assert percentile_of_value(breakpoints, 20.0) == 25.0


def test_percentile_of_value_below_range_clamps_to_lowest() -> None:
    breakpoints = {"0": 10.0, "50": 30.0, "100": 50.0}
    assert percentile_of_value(breakpoints, 0.0) == 0.0


def test_percentile_of_value_above_range_clamps_to_highest() -> None:
    breakpoints = {"0": 10.0, "50": 30.0, "100": 50.0}
    assert percentile_of_value(breakpoints, 999.0) == 100.0


def test_percentile_of_value_flat_segment_uses_lower_percentile() -> None:
    """A flat run (duplicate values across grid points) doesn't divide by zero."""
    breakpoints = {"0": 10.0, "50": 10.0, "100": 50.0}
    assert percentile_of_value(breakpoints, 10.0) == 0.0


def test_percentile_of_value_empty_breakpoints_raises() -> None:
    with pytest.raises(ValueError):
        percentile_of_value({}, 25.0)


def test_percentile_of_value_round_trips_compute_breakpoints() -> None:
    """Inverting compute_breakpoints' own output recovers the fitted percentile."""
    from app.services.sources.summer_league.cohort_baselines import compute_breakpoints

    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    bp = compute_breakpoints(values, percentiles=(0, 25, 50, 75, 100))
    for p_str, v in bp.items():
        assert percentile_of_value(bp, v) == float(p_str)


# --------------------------------------------------------------------------- #
# grade_for_percentile
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "pctl,expected",
    [
        (100.0, SummerLeagueDeskGrade.HOT),
        (90.0, SummerLeagueDeskGrade.HOT),
        (89.99, SummerLeagueDeskGrade.WARM),
        (65.0, SummerLeagueDeskGrade.WARM),
        (64.99, SummerLeagueDeskGrade.MID),
        (40.0, SummerLeagueDeskGrade.MID),
        (39.99, SummerLeagueDeskGrade.COLD),
        (0.0, SummerLeagueDeskGrade.COLD),
    ],
)
def test_grade_for_percentile_thresholds(
    pctl: float, expected: SummerLeagueDeskGrade
) -> None:
    """hot >=90 / warm 65-89 / mid 40-64 / cold <40, boundaries inclusive on the floor."""
    assert grade_for_percentile(pctl) == expected


# --------------------------------------------------------------------------- #
# gate_rung
# --------------------------------------------------------------------------- #
def test_gate_rung_clears_standard_gate() -> None:
    """A normal sample (2+ games, 60+ minutes) clears rung 0 -- the standard gate."""
    assert gate_rung(2, 60.0) == 0
    assert gate_rung(5, 150.0) == 0


def test_gate_rung_one_game_relaxed_minutes_only_clears_rung_1() -> None:
    """1 game with 20+ minutes clears the relaxed rung, not the standard gate."""
    assert gate_rung(1, 25.0) == 1


def test_gate_rung_one_game_low_minutes_clears_only_floor_rung() -> None:
    assert gate_rung(1, 5.0) == 2


def test_gate_rung_no_games_clears_nothing() -> None:
    assert gate_rung(0, 0.0) is None


def test_gate_rung_matches_leaders_ladder_constant() -> None:
    """Confirms the reused ladder is exactly the shipped Leaders GATE_LADDER."""
    assert GATE_LADDER == ((2, 60), (1, 20), (1, 0))


# --------------------------------------------------------------------------- #
# is_gated -- the adaptive gate-ladder suppression decision
# --------------------------------------------------------------------------- #
def test_is_gated_false_for_confident_sample_and_healthy_cohort() -> None:
    rung = gate_rung(2, 60.0)
    assert is_gated(rung, n_members=TARGET_BOARD_ROWS) is False
    assert is_gated(rung, n_members=TARGET_BOARD_ROWS + 50) is False


def test_is_gated_true_for_one_game_sample_even_in_a_healthy_cohort() -> None:
    """A 1-game sample is gated (no confident pctl) regardless of cohort size."""
    rung = gate_rung(1, 25.0)
    assert is_gated(rung, n_members=1000) is True


def test_is_gated_true_for_one_game_sample_in_a_thin_cohort() -> None:
    """The #503 DoD scenario: a 1-game sample in a thin cohort is gated."""
    rung = gate_rung(1, 5.0)
    assert is_gated(rung, n_members=3) is True


def test_is_gated_true_when_cohort_is_thin_even_with_a_confident_sample() -> None:
    """A healthy subject sample can't rescue a cohort too small to trust."""
    rung = gate_rung(5, 150.0)
    assert is_gated(rung, n_members=TARGET_BOARD_ROWS - 1) is True


def test_is_gated_true_when_rung_is_none() -> None:
    assert is_gated(None, n_members=1000) is True
