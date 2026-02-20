"""Unit tests for podcast summarization response parsing."""

import pytest

from app.schemas.podcast_episodes import PodcastEpisodeTag
from app.services.podcast_summarization_service import (
    EpisodeAnalysis,
    _parse_analysis_response,
    _parse_relevance_response,
)


class TestParseAnalysisResponse:
    """Tests for _parse_analysis_response."""

    def test_valid_json(self) -> None:
        """Valid JSON with all fields parses correctly."""
        text = '{"summary": "Great ep", "tag": "Interview", "mentioned_players": ["Cooper Flagg"]}'
        result = _parse_analysis_response(text)
        assert result.summary == "Great ep"
        assert result.tag == PodcastEpisodeTag.INTERVIEW
        assert result.mentioned_players == ["Cooper Flagg"]

    def test_empty_players(self) -> None:
        """Empty mentioned_players list is valid."""
        text = '{"summary": "Overview", "tag": "Draft Analysis", "mentioned_players": []}'
        result = _parse_analysis_response(text)
        assert result.mentioned_players == []

    def test_missing_players_key(self) -> None:
        """Missing mentioned_players key defaults to empty list."""
        text = '{"summary": "Overview", "tag": "Mock Draft"}'
        result = _parse_analysis_response(text)
        assert result.mentioned_players == []
        assert result.tag == PodcastEpisodeTag.MOCK_DRAFT

    def test_non_list_players(self) -> None:
        """Non-list mentioned_players is treated as empty."""
        text = '{"summary": "s", "tag": "Interview", "mentioned_players": "Cooper Flagg"}'
        result = _parse_analysis_response(text)
        assert result.mentioned_players == []

    def test_non_string_entries_filtered(self) -> None:
        """Non-string entries in mentioned_players are filtered out."""
        text = '{"summary": "s", "tag": "Interview", "mentioned_players": ["Cooper Flagg", 42, null]}'
        result = _parse_analysis_response(text)
        assert result.mentioned_players == ["Cooper Flagg"]

    def test_whitespace_entries_filtered(self) -> None:
        """Whitespace-only entries in mentioned_players are filtered out."""
        text = '{"summary": "s", "tag": "Interview", "mentioned_players": ["Cooper Flagg", "  ", ""]}'
        result = _parse_analysis_response(text)
        assert result.mentioned_players == ["Cooper Flagg"]

    def test_markdown_code_block(self) -> None:
        """Response wrapped in markdown code fences is parsed correctly."""
        text = '```json\n{"summary": "Great ep", "tag": "Interview", "mentioned_players": []}\n```'
        result = _parse_analysis_response(text)
        assert result.summary == "Great ep"
        assert result.tag == PodcastEpisodeTag.INTERVIEW

    def test_invalid_json_raises_value_error(self) -> None:
        """Non-JSON response raises ValueError."""
        with pytest.raises(ValueError, match="Invalid JSON"):
            _parse_analysis_response("not json at all")

    def test_unknown_tag_defaults(self) -> None:
        """Unknown tag string defaults to DRAFT_ANALYSIS."""
        text = '{"summary": "s", "tag": "Unknown Tag", "mentioned_players": []}'
        result = _parse_analysis_response(text)
        assert result.tag == PodcastEpisodeTag.DRAFT_ANALYSIS

    @pytest.mark.parametrize(
        "tag_str,expected",
        [
            ("Interview", PodcastEpisodeTag.INTERVIEW),
            ("Draft Analysis", PodcastEpisodeTag.DRAFT_ANALYSIS),
            ("Mock Draft", PodcastEpisodeTag.MOCK_DRAFT),
            ("Game Breakdown", PodcastEpisodeTag.GAME_BREAKDOWN),
            ("Trade & Intel", PodcastEpisodeTag.TRADE_INTEL),
            ("Prospect Debate", PodcastEpisodeTag.PROSPECT_DEBATE),
            ("Mailbag", PodcastEpisodeTag.MAILBAG),
            ("Event Preview", PodcastEpisodeTag.EVENT_PREVIEW),
        ],
    )
    def test_all_valid_tags(self, tag_str: str, expected: PodcastEpisodeTag) -> None:
        """All 8 valid tag strings map to the correct enum value."""
        text = f'{{"summary": "s", "tag": "{tag_str}", "mentioned_players": []}}'
        result = _parse_analysis_response(text)
        assert result.tag == expected

    def test_whitespace_around_json(self) -> None:
        """Leading/trailing whitespace is handled."""
        text = '  \n  {"summary": "s", "tag": "Mailbag", "mentioned_players": []}  \n  '
        result = _parse_analysis_response(text)
        assert result.tag == PodcastEpisodeTag.MAILBAG


class TestParseRelevanceResponse:
    """Tests for _parse_relevance_response."""

    def test_true(self) -> None:
        """Draft-relevant response returns True."""
        assert _parse_relevance_response('{"is_draft_relevant": true}') is True

    def test_false(self) -> None:
        """Non-relevant response returns False."""
        assert _parse_relevance_response('{"is_draft_relevant": false}') is False

    def test_missing_key(self) -> None:
        """Missing key defaults to False."""
        assert _parse_relevance_response('{"other_key": true}') is False

    def test_invalid_json(self) -> None:
        """Invalid JSON returns False."""
        assert _parse_relevance_response("not json") is False

    def test_markdown_code_block(self) -> None:
        """Response wrapped in markdown fences is parsed correctly."""
        text = '```json\n{"is_draft_relevant": true}\n```'
        assert _parse_relevance_response(text) is True
