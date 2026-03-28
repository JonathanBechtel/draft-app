"""Unit tests for combine score computation logic.

Tests the pure functions in compute_combine_scores that do weight
renormalization, height consolidation, category scoring, overall scoring,
and rank/percentile computation.
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from app.cli.compute_combine_scores import (
    ANTHRO_WEIGHTS,
    ATHLETIC_WEIGHTS,
    CATEGORY_WEIGHTS,
    SHOOTING_WEIGHTS,
    compute_category_score,
    compute_overall_score,
    rank_and_percentile,
    renormalize_weights,
    resolve_height_z,
    weighted_mean_z,
)


class TestRenormalizeWeights:
    """Tests for renormalize_weights."""

    def test_all_keys_present(self) -> None:
        """All keys present — weights unchanged except for normalization."""
        weights = {"a": 0.6, "b": 0.4}
        result = renormalize_weights(weights, {"a", "b"})
        assert result == pytest.approx({"a": 0.6, "b": 0.4})

    def test_subset_renormalizes(self) -> None:
        """Missing keys cause renormalization so remaining sum to 1."""
        weights = {"a": 0.6, "b": 0.2, "c": 0.2}
        result = renormalize_weights(weights, {"a", "b"})
        assert sum(result.values()) == pytest.approx(1.0)
        assert result["a"] == pytest.approx(0.75)
        assert result["b"] == pytest.approx(0.25)

    def test_no_overlap_returns_empty(self) -> None:
        """No overlapping keys returns empty dict."""
        result = renormalize_weights({"a": 1.0}, {"b", "c"})
        assert result == {}

    def test_single_key(self) -> None:
        """Single available key gets weight 1.0."""
        result = renormalize_weights({"a": 0.3, "b": 0.7}, {"a"})
        assert result == {"a": pytest.approx(1.0)}

    def test_predefined_weights_sum_to_one(self) -> None:
        """All predefined weight dicts sum to 1.0."""
        assert sum(ANTHRO_WEIGHTS.values()) == pytest.approx(1.0)
        assert sum(ATHLETIC_WEIGHTS.values()) == pytest.approx(1.0)
        assert sum(SHOOTING_WEIGHTS.values()) == pytest.approx(1.0)
        assert sum(CATEGORY_WEIGHTS.values()) == pytest.approx(1.0)


class TestWeightedMeanZ:
    """Tests for weighted_mean_z."""

    def test_simple_weighted_mean(self) -> None:
        """Correctly computes weighted mean of z-scores."""
        z = {"a": 1.0, "b": -1.0}
        w = {"a": 0.75, "b": 0.25}
        result = weighted_mean_z(z, w)
        assert result == pytest.approx(0.5)

    def test_missing_metric_renormalizes(self) -> None:
        """When a metric is missing, remaining weights renormalize."""
        z = {"a": 2.0}
        w = {"a": 0.5, "b": 0.5}
        result = weighted_mean_z(z, w)
        assert result == pytest.approx(2.0)  # only 'a' at 100% weight

    def test_no_overlap_returns_none(self) -> None:
        """No overlapping keys returns None."""
        result = weighted_mean_z({"a": 1.0}, {"b": 1.0})
        assert result is None

    def test_equal_weights(self) -> None:
        """Equal weights produce simple mean."""
        z = {"a": 1.0, "b": 3.0}
        w = {"a": 0.5, "b": 0.5}
        result = weighted_mean_z(z, w)
        assert result == pytest.approx(2.0)


class TestResolveHeightZ:
    """Tests for resolve_height_z (barefoot preferred, shoes fallback)."""

    def test_prefers_barefoot(self) -> None:
        """Uses barefoot height when both are available."""
        z = {"height_wo_shoes_in": 1.5, "height_w_shoes_in": 1.2}
        assert resolve_height_z(z) == 1.5

    def test_falls_back_to_shoes(self) -> None:
        """Falls back to shoes height when barefoot is missing."""
        z = {"height_w_shoes_in": 1.2}
        assert resolve_height_z(z) == 1.2

    def test_returns_none_when_missing(self) -> None:
        """Returns None when neither height metric is present."""
        assert resolve_height_z({"wingspan_in": 1.0}) is None


class TestComputeCategoryScore:
    """Tests for compute_category_score."""

    def test_full_anthro_data(self) -> None:
        """Category score with full anthropometric data uses all weights."""
        player_z = {
            "wingspan_in": 1.0,
            "standing_reach_in": 1.0,
            "height_wo_shoes_in": 1.0,
            "weight_lb": 1.0,
            "body_fat_pct": 1.0,
            "hand_length_in": 1.0,
            "hand_width_in": 1.0,
        }
        score, detail = compute_category_score(
            player_z, ANTHRO_WEIGHTS, "anthropometrics"
        )
        # All z-scores are 1.0, so weighted mean should be 1.0
        assert score == pytest.approx(1.0)
        assert detail["metric_count"] == 7
        assert "height" in detail["components"]

    def test_partial_data_renormalizes(self) -> None:
        """Score computed from partial data with renormalized weights."""
        player_z = {
            "wingspan_in": 2.0,
            "standing_reach_in": 0.0,
        }
        score, detail = compute_category_score(
            player_z, ANTHRO_WEIGHTS, "anthropometrics"
        )
        assert score is not None
        # Only wingspan (0.275) and reach (0.275), renormalized to 0.5 each
        assert score == pytest.approx(1.0)  # 2.0 * 0.5 + 0.0 * 0.5
        assert detail["metric_count"] == 2

    def test_height_consolidation_in_category(self) -> None:
        """Height metric in anthropometrics uses the consolidated resolver."""
        player_z = {"height_wo_shoes_in": 1.5, "height_w_shoes_in": 1.0}
        score, detail = compute_category_score(
            player_z, ANTHRO_WEIGHTS, "anthropometrics"
        )
        assert score is not None
        # Should use barefoot (1.5), not shoes (1.0)
        assert detail["components"]["height"]["z_score"] == pytest.approx(1.5)

    def test_no_data_returns_none(self) -> None:
        """No matching z-scores returns None score."""
        score, detail = compute_category_score(
            {"unrelated_metric": 1.0}, ANTHRO_WEIGHTS, "anthropometrics"
        )
        assert score is None
        assert detail["metric_count"] == 0


class TestComputeOverallScore:
    """Tests for compute_overall_score."""

    def test_all_three_categories(self) -> None:
        """Overall score with all 3 categories uses configured weights."""
        cat_scores = {
            "anthropometrics": 1.0,
            "combine_performance": 1.0,
            "shooting": 1.0,
        }
        score, detail = compute_overall_score(cat_scores, {})
        assert score == pytest.approx(1.0)
        assert detail["category_count"] == 3

    def test_no_shooting_reweights(self) -> None:
        """Without shooting, anthro and athletic split 50/50."""
        cat_scores = {
            "anthropometrics": 2.0,
            "combine_performance": 0.0,
        }
        score, detail = compute_overall_score(cat_scores, {})
        # 0.40/(0.40+0.40) = 0.50 each
        assert score == pytest.approx(1.0)
        assert detail["category_count"] == 2

    def test_shooting_weight_is_20_pct(self) -> None:
        """Shooting gets 20% weight with all categories present."""
        cat_scores = {
            "anthropometrics": 0.0,
            "combine_performance": 0.0,
            "shooting": 5.0,
        }
        score, _ = compute_overall_score(cat_scores, {})
        # 0.0*0.4 + 0.0*0.4 + 5.0*0.2 = 1.0
        assert score == pytest.approx(1.0)

    def test_single_category(self) -> None:
        """Single category gets full weight."""
        cat_scores = {"anthropometrics": 1.5}
        score, detail = compute_overall_score(cat_scores, {})
        assert score == pytest.approx(1.5)
        assert detail["category_count"] == 1

    def test_empty_returns_none(self) -> None:
        """No categories returns None."""
        score, detail = compute_overall_score({}, {})
        assert score is None


class TestRankAndPercentile:
    """Tests for rank_and_percentile."""

    def test_basic_ranking(self) -> None:
        """Higher raw score gets lower rank and higher percentile."""
        scores = pd.Series([1.0, 2.0, 3.0])
        result = rank_and_percentile(scores)
        assert list(result["rank"]) == [3, 2, 1]
        assert result["percentile"].iloc[2] > result["percentile"].iloc[0]

    def test_single_player(self) -> None:
        """Single player gets rank 1 and percentile 100."""
        scores = pd.Series([0.5])
        result = rank_and_percentile(scores)
        assert result["rank"].iloc[0] == 1
        assert result["percentile"].iloc[0] == pytest.approx(100.0)

    def test_empty_series(self) -> None:
        """Empty series returns empty DataFrame."""
        result = rank_and_percentile(pd.Series([], dtype=float))
        assert len(result) == 0

    def test_tied_scores(self) -> None:
        """Tied scores receive the same rank."""
        scores = pd.Series([1.0, 1.0, 2.0])
        result = rank_and_percentile(scores)
        # Both 1.0 scores should have the same rank
        assert result["rank"].iloc[0] == result["rank"].iloc[1]
        # The 2.0 score should be rank 1
        assert result["rank"].iloc[2] == 1

    def test_percentile_bounds(self) -> None:
        """All percentiles are between 0 and 100."""
        scores = pd.Series([-5.0, -2.0, 0.0, 1.0, 3.0])
        result = rank_and_percentile(scores)
        assert (result["percentile"] >= 0).all()
        assert (result["percentile"] <= 100).all()


class TestCategoryWeightRenormalization:
    """Integration-style tests for the category weight renormalization logic."""

    def test_anthro_athletic_only_is_fifty_fifty(self) -> None:
        """Without shooting, anthro and athletic each get 50%."""
        normed = renormalize_weights(
            CATEGORY_WEIGHTS, {"anthropometrics", "combine_performance"}
        )
        assert normed["anthropometrics"] == pytest.approx(0.5)
        assert normed["combine_performance"] == pytest.approx(0.5)

    def test_all_three_preserves_original(self) -> None:
        """With all three, original weights are preserved."""
        normed = renormalize_weights(
            CATEGORY_WEIGHTS,
            {"anthropometrics", "combine_performance", "shooting"},
        )
        assert normed["anthropometrics"] == pytest.approx(0.4)
        assert normed["combine_performance"] == pytest.approx(0.4)
        assert normed["shooting"] == pytest.approx(0.2)
