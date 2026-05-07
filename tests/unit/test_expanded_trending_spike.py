"""Unit tests for the trailing-window spike classifier."""

from __future__ import annotations

import pytest

from app.services.expanded_trending_service import _compute_spike_state


class TestComputeSpikeState:
    """Verify Hot / Cooling / neutral classifications."""

    def test_returns_none_for_empty_series(self) -> None:
        assert _compute_spike_state([]) is None

    def test_returns_none_for_short_series(self) -> None:
        # Service expects a 7-day series (one entry per day in window).
        assert _compute_spike_state([1, 2, 3]) is None

    def test_returns_hot_when_recent_rate_exceeds_prior_by_50pct(self) -> None:
        # Prior 5 days avg = 1; last 2 days avg = 2 -> ratio 2.0
        counts = [1, 1, 1, 1, 1, 2, 2]
        assert _compute_spike_state(counts) == "hot"

    def test_returns_cooling_when_recent_rate_drops_below_half(self) -> None:
        # Prior 5 days avg = 4; last 2 days avg = 1 -> ratio 0.25
        counts = [4, 4, 4, 4, 4, 1, 1]
        assert _compute_spike_state(counts) == "cooling"

    def test_returns_none_when_change_is_modest(self) -> None:
        counts = [3, 3, 3, 3, 3, 3, 4]  # ratio ~1.17
        assert _compute_spike_state(counts) is None

    def test_handles_zero_prior_history_with_strong_recent_burst(self) -> None:
        # No prior activity, but a clear burst in the last 2 days.
        counts = [0, 0, 0, 0, 0, 2, 2]
        assert _compute_spike_state(counts) == "hot"

    def test_handles_zero_prior_history_without_meaningful_burst(self) -> None:
        # No prior activity and only a single mention recently -> stay neutral.
        counts = [0, 0, 0, 0, 0, 1, 1]
        assert _compute_spike_state(counts) is None

    def test_zero_everywhere_is_neutral(self) -> None:
        assert _compute_spike_state([0, 0, 0, 0, 0, 0, 0]) is None

    @pytest.mark.parametrize(
        "counts,expected",
        [
            ([2, 2, 2, 2, 2, 3, 3], "hot"),  # ratio 1.5 exactly -> hot
            ([2, 2, 2, 2, 2, 1, 1], "cooling"),  # ratio 0.5 exactly -> cooling
        ],
    )
    def test_thresholds_are_inclusive(
        self, counts: list[int], expected: str
    ) -> None:
        assert _compute_spike_state(counts) == expected
