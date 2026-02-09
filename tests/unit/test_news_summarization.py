"""Unit tests for the news summarization service parse logic."""

from app.schemas.news_items import NewsItemTag
from app.services.news_summarization_service import NewsSummarizationService


class TestParseResponse:
    """Tests for NewsSummarizationService._parse_response()."""

    def setup_method(self) -> None:
        self.service = NewsSummarizationService()

    def test_parse_with_mentioned_players(self) -> None:
        """Should extract mentioned_players from valid JSON."""
        response = '{"summary": "A deep dive.", "tag": "Scouting Report", "mentioned_players": ["Cooper Flagg", "Ace Bailey"]}'
        result = self.service._parse_response(response)
        assert result.summary == "A deep dive."
        assert result.tag == NewsItemTag.SCOUTING_REPORT
        assert result.mentioned_players == ["Cooper Flagg", "Ace Bailey"]

    def test_parse_without_mentioned_players(self) -> None:
        """Should default to empty list when mentioned_players is absent."""
        response = '{"summary": "A mock draft.", "tag": "Mock Draft"}'
        result = self.service._parse_response(response)
        assert result.summary == "A mock draft."
        assert result.tag == NewsItemTag.MOCK_DRAFT
        assert result.mentioned_players == []

    def test_parse_with_empty_mentioned_players(self) -> None:
        """Should handle empty mentioned_players list."""
        response = '{"summary": "General analysis.", "tag": "Big Board", "mentioned_players": []}'
        result = self.service._parse_response(response)
        assert result.mentioned_players == []

    def test_parse_with_non_list_mentioned_players(self) -> None:
        """Should handle non-list mentioned_players gracefully."""
        response = '{"summary": "Test.", "tag": "Big Board", "mentioned_players": "Cooper Flagg"}'
        result = self.service._parse_response(response)
        assert result.mentioned_players == []

    def test_parse_filters_non_string_entries(self) -> None:
        """Should filter out non-string entries from mentioned_players."""
        response = '{"summary": "Test.", "tag": "Big Board", "mentioned_players": ["Cooper Flagg", 42, null, "Ace Bailey"]}'
        result = self.service._parse_response(response)
        assert result.mentioned_players == ["Cooper Flagg", "Ace Bailey"]

    def test_parse_strips_whitespace_from_names(self) -> None:
        """Should strip whitespace from player names."""
        response = '{"summary": "Test.", "tag": "Big Board", "mentioned_players": ["  Cooper Flagg  ", "Ace Bailey"]}'
        result = self.service._parse_response(response)
        assert result.mentioned_players == ["Cooper Flagg", "Ace Bailey"]

    def test_parse_json_in_code_block(self) -> None:
        """Should handle JSON wrapped in markdown code blocks."""
        response = '```json\n{"summary": "Test.", "tag": "Big Board", "mentioned_players": ["Cooper Flagg"]}\n```'
        result = self.service._parse_response(response)
        assert result.mentioned_players == ["Cooper Flagg"]
