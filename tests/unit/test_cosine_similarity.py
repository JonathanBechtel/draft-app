"""Unit tests for ``cosine_similarity`` in ``player_search_service``.

Pure math tests — no DB, no network.  Verifies the helper function's
contract (return value in [0,1], correct formula, edge cases).
"""

from __future__ import annotations

import math

import pytest

from app.services.player_search_service import cosine_similarity


class TestCosineSimilarity:
    """Tests for the ``cosine_similarity`` pure helper."""

    def test_identical_unit_vectors_return_one(self) -> None:
        """Two identical unit vectors have cosine similarity == 1.0."""
        v = [1.0, 0.0, 0.0]
        assert cosine_similarity(v, v) == pytest.approx(1.0)

    def test_orthogonal_vectors_return_zero(self) -> None:
        """Orthogonal vectors have cosine similarity == 0.0."""
        a = [1.0, 0.0, 0.0]
        b = [0.0, 1.0, 0.0]
        assert cosine_similarity(a, b) == pytest.approx(0.0)

    def test_forty_five_degree_vectors(self) -> None:
        """Vectors at 45 degrees have cosine similarity == 1/sqrt(2)."""
        a = [1.0, 0.0]
        b = [1.0, 1.0]  # not normalised — but cosine is scale-invariant
        assert cosine_similarity(a, b) == pytest.approx(1.0 / math.sqrt(2), abs=1e-9)

    def test_scale_invariant(self) -> None:
        """Scaling a vector does not change the cosine similarity."""
        a = [1.0, 2.0, 3.0]
        b = [2.0, 4.0, 6.0]  # b = 2 * a
        assert cosine_similarity(a, b) == pytest.approx(1.0)

    def test_768_dim_unit_vectors(self) -> None:
        """Works correctly on 768-dimensional vectors (production size)."""
        dims = 768
        a = [0.0] * dims
        a[0] = 1.0
        b = [0.0] * dims
        b[1] = 1.0
        assert cosine_similarity(a, b) == pytest.approx(0.0)

    def test_768_dim_same_vector(self) -> None:
        """Identity on a 768-dim vector with non-trivial values."""
        dims = 768
        v = [float(i % 7) / 7.0 for i in range(dims)]
        result = cosine_similarity(v, v)
        assert result == pytest.approx(1.0, abs=1e-9)

    def test_zero_vector_returns_zero(self) -> None:
        """A zero vector should return 0.0 rather than divide-by-zero."""
        a = [0.0, 0.0, 0.0]
        b = [1.0, 2.0, 3.0]
        assert cosine_similarity(a, b) == pytest.approx(0.0)

    def test_both_zero_vectors_return_zero(self) -> None:
        """Two zero vectors both return 0.0."""
        z = [0.0, 0.0]
        assert cosine_similarity(z, z) == pytest.approx(0.0)

    def test_result_clamped_at_one(self) -> None:
        """Floating-point noise above 1.0 is clamped to 1.0."""
        # Simulate floating-point overshoot by passing equal vectors with
        # fractional values that may accumulate rounding errors.  We then
        # assert the result is in [0, 1].
        v = [0.1] * 768
        result = cosine_similarity(v, v)
        assert 0.0 <= result <= 1.0

    def test_raises_on_length_mismatch(self) -> None:
        """Vectors of different lengths should raise ValueError."""
        a = [1.0, 2.0]
        b = [1.0, 2.0, 3.0]
        with pytest.raises(ValueError, match="length mismatch"):
            cosine_similarity(a, b)
