"""Unit tests for film-room utility helpers."""

from app.services.video_service import (
    format_view_count,
    parse_iso8601_duration,
    parse_youtube_video_id,
)


def test_parse_youtube_video_id_watch_url() -> None:
    """Extract id from youtube watch URL."""
    assert parse_youtube_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_parse_youtube_video_id_short_url() -> None:
    """Extract id from youtu.be URL."""
    assert parse_youtube_video_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_parse_iso8601_duration() -> None:
    """Parse ISO-8601 duration to integer seconds."""
    assert parse_iso8601_duration("PT1H2M3S") == 3723
    assert parse_iso8601_duration("PT9M") == 540
    assert parse_iso8601_duration("PT45S") == 45


def test_format_view_count() -> None:
    """Format view counts into compact display values."""
    assert format_view_count(950) == "950"
    assert format_view_count(12_300) == "12.3K"
    assert format_view_count(5_400_000) == "5.4M"
