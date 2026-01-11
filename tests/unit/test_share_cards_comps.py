"""Unit tests for share card comps logic."""

from app.services.share_cards.model_builders import _filters_for_comparison_group


class TestFiltersForComparisonGroup:
    """Tests for _filters_for_comparison_group helper."""

    def test_current_draft_filters_by_draft_year(self):
        """Should filter to same draft year for current_draft pool."""
        same_draft_year, nba_only = _filters_for_comparison_group("current_draft")

        assert same_draft_year is True
        assert nba_only is False

    def test_current_nba_filters_to_active_nba(self):
        """Should filter to active NBA players for current_nba pool."""
        same_draft_year, nba_only = _filters_for_comparison_group("current_nba")

        assert same_draft_year is False
        assert nba_only is True

    def test_historical_pools_do_not_apply_filters(self):
        """Should not filter when pool is historical."""
        same_draft_year, nba_only = _filters_for_comparison_group("all_time_draft")

        assert same_draft_year is False
        assert nba_only is False

    def test_unknown_pool_defaults_to_no_filters(self):
        """Should not filter when pool is unknown."""
        same_draft_year, nba_only = _filters_for_comparison_group("something_else")

        assert same_draft_year is False
        assert nba_only is False

