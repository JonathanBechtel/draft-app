"""Unit tests for podcast ingestion service utilities."""

import pytest

from app.services.podcast_ingestion_service import (
    _parse_itunes_duration,
    check_keyword_relevance,
)


class TestCheckKeywordRelevance:
    """Tests for the check_keyword_relevance pure function."""

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
                "NFL Free Agency Recap", "Latest signings and trades in the NFL"
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


class TestParseItunesDuration:
    """Tests for the _parse_itunes_duration helper."""

    def test_hh_mm_ss(self) -> None:
        """HH:MM:SS format parses correctly."""
        assert _parse_itunes_duration({"itunes_duration": "1:02:03"}) == 3723

    def test_mm_ss(self) -> None:
        """MM:SS format parses correctly."""
        assert _parse_itunes_duration({"itunes_duration": "45:30"}) == 2730

    def test_raw_seconds(self) -> None:
        """Raw seconds string parses correctly."""
        assert _parse_itunes_duration({"itunes_duration": "2700"}) == 2700

    def test_empty_string(self) -> None:
        """Empty string returns None."""
        assert _parse_itunes_duration({"itunes_duration": ""}) is None

    def test_missing_field(self) -> None:
        """Missing field returns None."""
        assert _parse_itunes_duration({}) is None

    def test_invalid_format(self) -> None:
        """Invalid format returns None."""
        assert _parse_itunes_duration({"itunes_duration": "not-a-time"}) is None
