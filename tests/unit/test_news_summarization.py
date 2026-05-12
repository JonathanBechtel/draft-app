"""Unit tests for the news summarization service parse logic."""

from app.schemas.news_items import NewsItemTag
from app.services.news_summarization_service import (
    NewsSummarizationService,
    _parse_relevance_response,
)


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


class TestParseRelevanceResponse:
    """Tests for _parse_relevance_response (Gemini relevance gate parser)."""

    def test_true(self) -> None:
        """Draft-relevant payload returns True."""
        assert _parse_relevance_response('{"is_draft_relevant": true}') is True

    def test_false(self) -> None:
        """Non-relevant payload returns False."""
        assert _parse_relevance_response('{"is_draft_relevant": false}') is False

    def test_missing_key(self) -> None:
        """Missing key defaults to False."""
        assert _parse_relevance_response('{"other_key": true}') is False

    def test_invalid_json(self) -> None:
        """Invalid JSON returns False rather than raising."""
        assert _parse_relevance_response("not json") is False

    def test_markdown_code_block(self) -> None:
        """Response wrapped in markdown fences is parsed correctly."""
        text = '```json\n{"is_draft_relevant": true}\n```'
        assert _parse_relevance_response(text) is True

    def test_quoted_string_true_admits(self) -> None:
        """Models sometimes emit a quoted "true"; treat it as relevant."""
        assert _parse_relevance_response('{"is_draft_relevant": "true"}') is True
        assert _parse_relevance_response('{"is_draft_relevant": "TRUE"}') is True
        assert _parse_relevance_response('{"is_draft_relevant": " True "}') is True

    def test_quoted_string_false_fails_closed(self) -> None:
        """Quoted "false" is the bug guard — Python truthy bool() would admit it."""
        assert _parse_relevance_response('{"is_draft_relevant": "false"}') is False
        assert _parse_relevance_response('{"is_draft_relevant": "no"}') is False

    def test_non_true_values_fail_closed(self) -> None:
        """Numeric or other non-bool values are not affirmative."""
        assert _parse_relevance_response('{"is_draft_relevant": 1}') is False
        assert _parse_relevance_response('{"is_draft_relevant": 0}') is False
        assert _parse_relevance_response('{"is_draft_relevant": null}') is False
