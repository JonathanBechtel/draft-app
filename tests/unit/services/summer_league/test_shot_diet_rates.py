"""Unit tests for the compute_shot_diet helper in metrics.py.

These tests exercise the pure rate-computation function with no DB dependency:
- correct bucketing of the 6 included NBA SHOT_ZONE_BASIC values
- corner3 as a sub-fraction of three_rate
- NULL result when zone_fga is empty
- fractions stored to 4 decimal places
"""

from __future__ import annotations

import pytest

from app.services.sources.summer_league.metrics import compute_shot_diet


def test_compute_shot_diet_all_zones() -> None:
    """Each zone maps to the correct rate bucket.

    Input: 10 RA, 5 Paint Non-RA, 5 Mid-Range, 3 LC3, 3 RC3, 4 AB3 = 30 total.
    Expected:
      rim_rate     = 10/30
      mid_rate     = (5+5)/30 = 10/30
      three_rate   = (3+3+4)/30 = 10/30
      corner3_rate = (3+3)/30   =  6/30
    """
    zones = {
        "Restricted Area": 10,
        "In The Paint (Non-RA)": 5,
        "Mid-Range": 5,
        "Left Corner 3": 3,
        "Right Corner 3": 3,
        "Above the Break 3": 4,
    }
    result = compute_shot_diet(zones)

    assert result["rim_rate"] == pytest.approx(10 / 30, abs=1e-4)
    assert result["mid_rate"] == pytest.approx(10 / 30, abs=1e-4)
    assert result["three_rate"] == pytest.approx(10 / 30, abs=1e-4)
    assert result["corner3_rate"] == pytest.approx(6 / 30, abs=1e-4)


def test_compute_shot_diet_rim_only() -> None:
    """All shots from the restricted area → rim_rate=1.0, others 0."""
    result = compute_shot_diet({"Restricted Area": 20})

    assert result["rim_rate"] == pytest.approx(1.0, abs=1e-4)
    assert result["mid_rate"] == pytest.approx(0.0, abs=1e-4)
    assert result["three_rate"] == pytest.approx(0.0, abs=1e-4)
    assert result["corner3_rate"] == pytest.approx(0.0, abs=1e-4)


def test_compute_shot_diet_corner3_is_subset_of_three_rate() -> None:
    """corner3_rate must always be ≤ three_rate (LC3 + RC3 ⊂ all 3-pt zones)."""
    zones = {
        "Left Corner 3": 4,
        "Right Corner 3": 2,
        "Above the Break 3": 6,
        "Mid-Range": 8,
    }
    result = compute_shot_diet(zones)

    assert result["corner3_rate"] is not None
    assert result["three_rate"] is not None
    assert result["corner3_rate"] <= result["three_rate"]
    # Specific values: corner3 = 6/20 = 0.3, three = 12/20 = 0.6
    assert result["corner3_rate"] == pytest.approx(6 / 20, abs=1e-4)
    assert result["three_rate"] == pytest.approx(12 / 20, abs=1e-4)


def test_compute_shot_diet_empty_returns_nulls() -> None:
    """Empty zone dict (no shot data) → all None."""
    result = compute_shot_diet({})

    assert result["rim_rate"] is None
    assert result["mid_rate"] is None
    assert result["three_rate"] is None
    assert result["corner3_rate"] is None


def test_compute_shot_diet_backcourt_ignored_by_caller() -> None:
    """Backcourt zone label is not mapped → treated as unknown, not counted.

    The _load_shot_diet query excludes Backcourt rows before calling
    compute_shot_diet, but if a Backcourt entry somehow reaches this function
    it is silently ignored (not in _ZONE_TO_BUCKET or _CORNER3_ZONES).
    The denominator is computed from the supplied fga values, so if Backcourt
    IS in the dict it inflates the denominator but not any bucket numerator.
    """
    zones = {
        "Restricted Area": 5,
        "Backcourt": 2,  # should not land in any bucket
    }
    result = compute_shot_diet(zones)

    # Total = 7 (Backcourt counted in denominator since it's in zone_fga).
    # This is the expected behaviour when called directly; the real pipeline
    # excludes Backcourt at the DB level so zone_fga never contains it.
    assert result["rim_rate"] == pytest.approx(5 / 7, abs=1e-4)
    assert result["mid_rate"] == pytest.approx(0.0, abs=1e-4)
    assert result["three_rate"] == pytest.approx(0.0, abs=1e-4)


def test_compute_shot_diet_stored_as_fractions() -> None:
    """Rates are fractions (0–1), not percentages; rounded to 4 dp."""
    zones = {"Restricted Area": 1, "Mid-Range": 3}  # 25% rim, 75% mid
    result = compute_shot_diet(zones)

    assert result["rim_rate"] == 0.25
    assert result["mid_rate"] == 0.75
    # All values are in [0, 1]
    for key in ("rim_rate", "mid_rate", "three_rate", "corner3_rate"):
        val = result[key]
        assert val is not None
        assert 0.0 <= val <= 1.0
