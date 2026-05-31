"""Unit tests for the consensus alignment statistics.

Exercises the pure helpers that back the source "alignment score":
``_rankdata`` / ``_pearson`` / ``_spearman`` (computation) in
``consensus_service`` and ``_alignment_score`` (0–100 presentation) in
``consensus_read_service``. No DB required.
"""

from app.services.consensus_read_service import _alignment_score
from app.services.consensus_service import _pearson, _rankdata, _spearman


class TestRankdata:
    """``_rankdata`` returns 1-based ranks, averaging ties."""

    def test_strictly_increasing(self) -> None:
        assert _rankdata([10.0, 20.0, 30.0]) == [1.0, 2.0, 3.0]

    def test_unordered(self) -> None:
        # smallest gets rank 1 regardless of position
        assert _rankdata([30.0, 10.0, 20.0]) == [3.0, 1.0, 2.0]

    def test_ties_get_average_rank(self) -> None:
        # two tied values occupy ranks 1 and 2 → both get 1.5
        assert _rankdata([5.0, 5.0, 9.0]) == [1.5, 1.5, 3.0]


class TestPearson:
    """``_pearson`` correlation with the usual edge cases."""

    def test_perfect_positive(self) -> None:
        r = _pearson([1.0, 2.0, 3.0], [2.0, 4.0, 6.0])
        assert r is not None and abs(r - 1.0) < 1e-9

    def test_perfect_negative(self) -> None:
        r = _pearson([1.0, 2.0, 3.0], [3.0, 2.0, 1.0])
        assert r is not None and abs(r + 1.0) < 1e-9

    def test_constant_variable_is_undefined(self) -> None:
        assert _pearson([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]) is None

    def test_too_few_points(self) -> None:
        assert _pearson([1.0], [2.0]) is None


class TestSpearman:
    """``_spearman`` = Pearson on ranks; needs ≥3 shared points."""

    def test_identical_ordering_is_one(self) -> None:
        pairs = [(1, 1), (2, 2), (3, 3), (4, 4)]
        rho = _spearman(pairs)
        assert rho is not None and abs(rho - 1.0) < 1e-9

    def test_reversed_ordering_is_minus_one(self) -> None:
        pairs = [(1, 4), (2, 3), (3, 2), (4, 1)]
        rho = _spearman(pairs)
        assert rho is not None and abs(rho + 1.0) < 1e-9

    def test_monotonic_but_not_identical_is_perfect(self) -> None:
        # Spearman cares about order, not absolute spacing — a source that
        # preserves the consensus order (with gaps) still aligns perfectly.
        pairs = [(1, 1), (2, 2), (5, 3), (9, 4)]
        rho = _spearman(pairs)
        assert rho is not None and abs(rho - 1.0) < 1e-9

    def test_one_swap_reduces_correlation(self) -> None:
        pairs = [(1, 1), (2, 3), (3, 2), (4, 4)]
        rho = _spearman(pairs)
        assert rho is not None and 0.0 < rho < 1.0

    def test_fewer_than_three_pairs_is_none(self) -> None:
        assert _spearman([(1, 1), (2, 2)]) is None


class TestAlignmentScore:
    """``_alignment_score`` maps rho∈[-1,1] → 0–100, None passes through."""

    def test_none_passes_through(self) -> None:
        assert _alignment_score(None) is None

    def test_perfect_alignment_is_100(self) -> None:
        assert _alignment_score(1.0) == 100

    def test_anti_alignment_is_0(self) -> None:
        assert _alignment_score(-1.0) == 0

    def test_no_correlation_is_50(self) -> None:
        assert _alignment_score(0.0) == 50

    def test_clamps_out_of_range(self) -> None:
        # Defensive: values can't really exceed ±1, but the mapping clamps.
        assert _alignment_score(1.5) == 100
        assert _alignment_score(-1.5) == 0
