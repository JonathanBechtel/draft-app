"""Unit tests for the weekly-ceiling math in the consensus history driver.

Pure function; no DB. Verifies the ascending ceiling series always includes
the end bound and handles short/degenerate ranges.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from scripts.generate_consensus_history import _weekly_ceilings


def test_weekly_ceilings_includes_end_and_steps_by_interval() -> None:
    """A 28-day span at 7-day steps yields base, +7, +14, +21, +28."""
    base = datetime(2026, 1, 1)
    end = base + timedelta(days=28)
    out = _weekly_ceilings(base, end, 7)
    assert out == [base + timedelta(days=7 * i) for i in range(5)]


def test_weekly_ceilings_appends_partial_final_week() -> None:
    """An end that doesn't land on an interval boundary is still included."""
    base = datetime(2026, 1, 1)
    end = base + timedelta(days=10)  # between +7 and +14
    out = _weekly_ceilings(base, end, 7)
    assert out == [base, base + timedelta(days=7), end]


def test_weekly_ceilings_single_point_when_start_equals_end() -> None:
    base = datetime(2026, 1, 1)
    assert _weekly_ceilings(base, base, 7) == [base]


def test_weekly_ceilings_empty_when_end_before_start() -> None:
    base = datetime(2026, 1, 1)
    assert _weekly_ceilings(base, base - timedelta(days=1), 7) == []
