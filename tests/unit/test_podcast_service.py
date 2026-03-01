"""Unit tests for podcast service utilities."""

import pytest

from app.schemas.podcast_episodes import PodcastEpisodeTag
from app.services.podcast_service import (
    _coerce_podcast_tag,
    _resolve_podcast_tag,
    build_listen_on_text,
    format_duration,
)


class TestFormatDuration:
    """Tests for the format_duration utility function."""

    def test_minutes_and_seconds(self) -> None:
        """Standard duration formats as M:SS."""
        assert format_duration(2723) == "45:23"

    def test_exact_minutes(self) -> None:
        """Exact minute durations show :00 seconds."""
        assert format_duration(1800) == "30:00"

    def test_with_hours(self) -> None:
        """Durations over an hour format as H:MM:SS."""
        assert format_duration(3723) == "1:02:03"

    def test_exactly_one_hour(self) -> None:
        """Exactly 60 minutes formats as 1:00:00."""
        assert format_duration(3600) == "1:00:00"

    def test_zero_seconds(self) -> None:
        """Zero duration formats as 0:00."""
        assert format_duration(0) == "0:00"

    def test_under_one_minute(self) -> None:
        """Sub-minute durations still show M:SS."""
        assert format_duration(45) == "0:45"

    def test_none_returns_empty(self) -> None:
        """None duration returns empty string."""
        assert format_duration(None) == ""

    def test_negative_returns_empty(self) -> None:
        """Negative duration returns empty string."""
        assert format_duration(-1) == ""

    def test_large_duration(self) -> None:
        """Multi-hour durations format correctly."""
        assert format_duration(7384) == "2:03:04"


class TestBuildListenOnText:
    """Tests for the build_listen_on_text utility function."""

    def test_standard_show(self) -> None:
        """Generates expected CTA text for a standard show name."""
        assert build_listen_on_text("The Ringer") == "Listen on The Ringer"

    def test_long_show_name(self) -> None:
        """Handles longer show names."""
        assert (
            build_listen_on_text("NBA Draft Show with Sam Vecenie")
            == "Listen on NBA Draft Show with Sam Vecenie"
        )

    def test_empty_show_name(self) -> None:
        """Empty show name still produces valid text."""
        assert build_listen_on_text("") == "Listen on "


class TestPodcastTagHelpers:
    """Tests for podcast tag parsing/formatting helpers."""

    def test_coerce_from_display_value(self) -> None:
        """Display-value tags map to enum members."""
        assert _coerce_podcast_tag("Mock Draft") == PodcastEpisodeTag.MOCK_DRAFT

    def test_coerce_from_enum_name(self) -> None:
        """Enum-name tags map to enum members."""
        assert _coerce_podcast_tag("MOCK_DRAFT") == PodcastEpisodeTag.MOCK_DRAFT

    def test_coerce_invalid_returns_none(self) -> None:
        """Unknown tags return None instead of raising."""
        assert _coerce_podcast_tag("Unknown Podcast Tag") is None

    def test_resolve_from_enum_member(self) -> None:
        """Enum member resolves to display value."""
        assert _resolve_podcast_tag(PodcastEpisodeTag.TRADE_INTEL) == "Trade & Intel"

    def test_resolve_from_enum_name(self) -> None:
        """Enum-name strings resolve to display values."""
        assert _resolve_podcast_tag("TRADE_INTEL") == "Trade & Intel"

    def test_resolve_from_display_value(self) -> None:
        """Display-value strings round-trip cleanly."""
        assert _resolve_podcast_tag("Trade & Intel") == "Trade & Intel"

    def test_resolve_unknown_falls_back_to_raw(self) -> None:
        """Unknown strings do not crash the formatter."""
        assert _resolve_podcast_tag("UNMAPPED_TAG") == "UNMAPPED_TAG"
