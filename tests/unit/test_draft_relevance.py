"""Unit tests for the shared draft-relevance keyword filter."""

from app.utils.draft_relevance import check_keyword_relevance


class TestCheckKeywordRelevance:
    """Tests for the shared keyword pre-filter used by all ingestion services."""

    def test_match_title_nba_draft(self) -> None:
        """Title containing 'nba draft' is relevant."""
        assert check_keyword_relevance("2025 NBA Draft Preview", "") is True

    def test_match_title_mock_draft(self) -> None:
        """Title containing 'mock draft' is relevant."""
        assert check_keyword_relevance("Our Latest Mock Draft", "") is True

    def test_match_description_combine(self) -> None:
        """Description containing 'combine' is relevant."""
        assert (
            check_keyword_relevance(
                "Sports Update", "Previewing the upcoming combine results"
            )
            is True
        )

    def test_match_description_prospect(self) -> None:
        """Description containing 'prospect' is relevant."""
        assert (
            check_keyword_relevance(
                "Basketball Talk", "Breaking down the top prospect in college hoops"
            )
            is True
        )

    def test_no_match(self) -> None:
        """Unrelated content is not relevant."""
        assert (
            check_keyword_relevance(
                "Election Polls Tighten",
                "Pollsters revisit their state-by-state models",
            )
            is False
        )

    def test_case_insensitive(self) -> None:
        """Keyword matching is case insensitive."""
        assert check_keyword_relevance("NBA DRAFT Analysis", "") is True
        assert check_keyword_relevance("nba draft analysis", "") is True
        assert check_keyword_relevance("Nba Draft Analysis", "") is True

    def test_empty_inputs(self) -> None:
        """Empty title and description are not relevant."""
        assert check_keyword_relevance("", "") is False
