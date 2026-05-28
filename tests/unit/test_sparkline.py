"""Unit tests for the sparkline path / direction helpers."""

from __future__ import annotations

from app.utils.sparkline import build_sparkline_path, sparkline_direction


def test_build_sparkline_path_returns_none_for_empty_series() -> None:
    """No data → no line to draw."""
    assert build_sparkline_path([]) is None


def test_build_sparkline_path_returns_none_for_single_point() -> None:
    """A single rank has no trajectory."""
    assert build_sparkline_path([5]) is None


def test_build_sparkline_path_two_points_traces_line() -> None:
    """Two ranks produce a single ``M ... L ...`` segment within the box."""
    path = build_sparkline_path([3, 7], width=80, height=18, padding=2)
    assert path is not None
    assert path.startswith("M ")
    assert " L " in path
    # Each coordinate stays inside the [padding, dim - padding] window.
    assert "2.0,2.0" in path  # first point: leftmost x, top y (best rank)
    assert "78.0,16.0" in path  # last point: rightmost x, bottom y (worst rank)


def test_build_sparkline_path_handles_flat_series_without_dividing_by_zero() -> None:
    """A perfectly flat series collapses to the top edge without crashing."""
    path = build_sparkline_path([4, 4, 4, 4])
    assert path is not None
    # All y values should be the padding (top), since (r - lo) / rng = 0.
    assert "L 27.3,2.0" in path or "L 27.4,2.0" in path  # midpoint x with top y


def test_sparkline_direction_up_when_rank_improves() -> None:
    """Series ending below where it started = improvement → 'up'."""
    assert sparkline_direction([10, 8, 6, 5]) == "up"


def test_sparkline_direction_down_when_rank_worsens() -> None:
    """Series ending above where it started = worsening → 'down'."""
    assert sparkline_direction([3, 5, 8]) == "down"


def test_sparkline_direction_flat_for_unchanged_endpoints() -> None:
    """Same first and last rank → 'flat' regardless of intermediate motion."""
    assert sparkline_direction([5, 8, 5]) == "flat"


def test_sparkline_direction_flat_for_insufficient_data() -> None:
    """Empty or single-point series → 'flat' (no trend to report)."""
    assert sparkline_direction([]) == "flat"
    assert sparkline_direction([7]) == "flat"
