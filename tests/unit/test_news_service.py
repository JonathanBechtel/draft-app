"""Unit tests for news service helper functions."""

from datetime import datetime, timedelta, timezone


from app.services.news_service import (
    build_read_more_text,
    format_relative_time,
)


class TestFormatRelativeTime:
    """Tests for format_relative_time() function."""

    def test_formats_minutes_recently(self):
        """Recent times should show as minutes."""
        now = datetime.now(timezone.utc)
        five_min_ago = now - timedelta(minutes=5)
        assert format_relative_time(five_min_ago) == "5m"

    def test_formats_one_minute_minimum(self):
        """Times less than 1 minute should show as 1m."""
        now = datetime.now(timezone.utc)
        just_now = now - timedelta(seconds=30)
        assert format_relative_time(just_now) == "1m"

    def test_formats_hours(self):
        """Times over an hour should show as hours."""
        now = datetime.now(timezone.utc)
        two_hours_ago = now - timedelta(hours=2)
        assert format_relative_time(two_hours_ago) == "2h"

    def test_formats_days(self):
        """Times over a day should show as days."""
        now = datetime.now(timezone.utc)
        three_days_ago = now - timedelta(days=3)
        assert format_relative_time(three_days_ago) == "3d"

    def test_formats_weeks(self):
        """Times over a week should show as weeks."""
        now = datetime.now(timezone.utc)
        two_weeks_ago = now - timedelta(weeks=2)
        assert format_relative_time(two_weeks_ago) == "2w"

    def test_handles_naive_datetime(self):
        """Naive datetimes should be treated as UTC."""
        # Create a naive datetime that represents 1 hour ago
        naive_dt = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
        result = format_relative_time(naive_dt)
        assert result == "1h"

    def test_future_time_shows_now(self):
        """Future times should show as 'now'."""
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        assert format_relative_time(future) == "now"

    def test_exactly_one_hour(self):
        """Exactly 60 minutes should show as 1h."""
        now = datetime.now(timezone.utc)
        one_hour_ago = now - timedelta(minutes=60)
        assert format_relative_time(one_hour_ago) == "1h"

    def test_exactly_24_hours(self):
        """Exactly 24 hours should show as 1d."""
        now = datetime.now(timezone.utc)
        one_day_ago = now - timedelta(hours=24)
        assert format_relative_time(one_day_ago) == "1d"


class TestBuildReadMoreText:
    """Tests for build_read_more_text() function."""

    def test_generates_cta_text(self):
        """Should generate 'Read at [source]' text."""
        result = build_read_more_text("Floor and Ceiling")
        assert result == "Read at Floor and Ceiling"

    def test_handles_short_names(self):
        """Should work with short source names."""
        result = build_read_more_text("ESPN")
        assert result == "Read at ESPN"

    def test_handles_empty_name(self):
        """Should handle empty source name gracefully."""
        result = build_read_more_text("")
        assert result == "Read at "
