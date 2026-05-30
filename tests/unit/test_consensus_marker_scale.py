"""Unit tests for the consensus board range-bar marker-scale guard.

The board partial computes a per-row scale that folds ``consensus_rank`` into
[high_rank, low_rank] so the marker never renders outside the track.  This
mirrors the PR #267 guard used in the homepage controversy panel.

The guard logic (pure Python, mirroring the Jinja template):

    scale_min = min(high_rank, consensus_rank)
    scale_max = max(low_rank, consensus_rank)
    span      = scale_max - scale_min or 1
    marker_pct = (consensus_rank - scale_min) / span * 100

Expected invariant: 0 <= marker_pct <= 100 for all valid inputs.
"""

from __future__ import annotations


def _marker_pct(consensus_rank: int, high_rank: int, low_rank: int) -> float:
    """Return the marker percentage for a consensus board range bar.

    Mirrors the Jinja calculation in ``board.html``:
        row_scale_min = min(high_rank, consensus_rank)
        row_scale_max = max(low_rank,  consensus_rank)
        row_span      = (row_scale_max - row_scale_min) or 1
        marker_pct    = (consensus_rank - row_scale_min) / row_span * 100

    Args:
        consensus_rank: The blended consensus rank (e.g. 5).
        high_rank: The best (lowest number) any single source assigned.
        low_rank: The worst (highest number) any single source assigned.

    Returns:
        A float in [0, 100] representing the marker's horizontal position.
    """
    scale_min = min(high_rank, consensus_rank)
    scale_max = max(low_rank, consensus_rank)
    span = (scale_max - scale_min) or 1
    return (consensus_rank - scale_min) / span * 100


# ---------------------------------------------------------------------------
# Happy-path tests: consensus_rank within [high_rank, low_rank]
# ---------------------------------------------------------------------------


def test_marker_at_high_end_returns_zero() -> None:
    """When consensus == high_rank (best), marker sits at 0% (left edge)."""
    assert _marker_pct(consensus_rank=3, high_rank=3, low_rank=8) == 0.0


def test_marker_at_low_end_returns_100() -> None:
    """When consensus == low_rank (worst), marker sits at 100% (right edge)."""
    assert _marker_pct(consensus_rank=8, high_rank=3, low_rank=8) == 100.0


def test_marker_at_midpoint() -> None:
    """Consensus at the midpoint of [high, low] should be exactly 50%."""
    pct = _marker_pct(consensus_rank=5, high_rank=3, low_rank=7)
    assert abs(pct - 50.0) < 0.01


def test_marker_within_range_arbitrary() -> None:
    """Marker pct is in [0, 100] for a typical mid-board player."""
    pct = _marker_pct(consensus_rank=12, high_rank=9, low_rank=16)
    assert 0.0 <= pct <= 100.0


# ---------------------------------------------------------------------------
# Guard tests: consensus_rank OUTSIDE [high_rank, low_rank] — the key case
# that the PR #267 guard must handle without producing < 0 or > 100.
# ---------------------------------------------------------------------------


def test_marker_clamped_when_consensus_above_high() -> None:
    """consensus_rank < high_rank (ranked better than any source) → 0%.

    Without the guard the formula would yield a negative percentage,
    rendering the marker to the left of the track.
    """
    pct = _marker_pct(consensus_rank=2, high_rank=5, low_rank=10)
    assert pct == 0.0, f"Expected 0.0, got {pct}"


def test_marker_clamped_when_consensus_below_low() -> None:
    """consensus_rank > low_rank (ranked worse than any source) → 100%.

    Without the guard the formula would yield > 100%, rendering the marker
    to the right of the track.
    """
    pct = _marker_pct(consensus_rank=15, high_rank=5, low_rank=10)
    assert pct == 100.0, f"Expected 100.0, got {pct}"


def test_marker_stays_in_bounds_consensus_far_above_high() -> None:
    """Consensus rank much better than any source: marker at left edge."""
    pct = _marker_pct(consensus_rank=1, high_rank=8, low_rank=20)
    assert 0.0 <= pct <= 100.0


def test_marker_stays_in_bounds_consensus_far_below_low() -> None:
    """Consensus rank much worse than any source: marker at right edge."""
    pct = _marker_pct(consensus_rank=30, high_rank=8, low_rank=15)
    assert 0.0 <= pct <= 100.0


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_single_source_high_equals_low() -> None:
    """One-source player: high == low.  span = 1 avoids division by zero.

    When high_rank == low_rank == consensus_rank the span would be zero
    without the ``or 1`` guard; the marker should render at 0%.
    """
    pct = _marker_pct(consensus_rank=7, high_rank=7, low_rank=7)
    # scale_min = scale_max = 7, span = 0 → guarded to 1
    # (7 - 7) / 1 * 100 = 0.0
    assert pct == 0.0


def test_single_source_consensus_differs_from_both() -> None:
    """One-source player where consensus_rank != source rank (avg effect).

    high_rank == low_rank = 10 but consensus_rank = 8 (blending effect).
    Guard folds consensus into scale → marker at 0% (left edge).
    """
    pct = _marker_pct(consensus_rank=8, high_rank=10, low_rank=10)
    # scale_min = min(10, 8) = 8, scale_max = max(10, 8) = 10, span = 2
    # (8 - 8) / 2 * 100 = 0.0
    assert pct == 0.0


def test_all_ranks_equal_no_zero_division() -> None:
    """All three ranks equal: span guard prevents division by zero."""
    pct = _marker_pct(consensus_rank=5, high_rank=5, low_rank=5)
    assert pct == 0.0


def test_large_board_values_stay_in_bounds() -> None:
    """Large rank values (deep board) keep the marker in bounds."""
    pct = _marker_pct(consensus_rank=55, high_rank=40, low_rank=70)
    assert 0.0 <= pct <= 100.0
