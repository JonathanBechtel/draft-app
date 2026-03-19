"""Unit tests for the player enrichment service."""

from __future__ import annotations

from datetime import date

import pytest

from app.services.player_enrichment_service import _parse_date, _parse_json_response


class TestParseJsonResponse:
    """Tests for JSON response parsing from Gemini."""

    def test_valid_json(self) -> None:
        """Parses well-formed JSON dict."""
        result = _parse_json_response('{"confidence": "high", "school": "Duke"}')
        assert result == {"confidence": "high", "school": "Duke"}

    def test_strips_markdown_fences(self) -> None:
        """Removes ```json ... ``` wrapper."""
        result = _parse_json_response('```json\n{"school": "BYU"}\n```')
        assert result == {"school": "BYU"}

    def test_strips_bare_fences(self) -> None:
        """Removes ``` ... ``` wrapper without json tag."""
        result = _parse_json_response('```\n{"school": "BYU"}\n```')
        assert result == {"school": "BYU"}

    def test_invalid_json_returns_none(self) -> None:
        """Returns None for unparseable text."""
        assert _parse_json_response("not json at all") is None

    def test_non_dict_returns_none(self) -> None:
        """Returns None if JSON parses to a non-dict (e.g. list)."""
        assert _parse_json_response("[1, 2, 3]") is None

    def test_empty_string_returns_none(self) -> None:
        """Returns None for empty input."""
        assert _parse_json_response("") is None


class TestParseDate:
    """Tests for date string parsing."""

    def test_valid_date(self) -> None:
        """Parses YYYY-MM-DD format."""
        assert _parse_date("2007-01-29") == date(2007, 1, 29)

    def test_none_input(self) -> None:
        """Returns None for None."""
        assert _parse_date(None) is None

    def test_empty_string(self) -> None:
        """Returns None for empty string."""
        assert _parse_date("") is None

    def test_invalid_format(self) -> None:
        """Returns None for non-date strings."""
        assert _parse_date("January 29, 2007") is None

    def test_partial_date(self) -> None:
        """Returns None for incomplete dates."""
        assert _parse_date("2007-01") is None
