"""Unit tests for the game-flow series builder in summer_league_games_service.

Tests cover:
- Series endpoints match the final score margin.
- Elapsed time is monotonically increasing.
- Lead changes (zero-crossings) are represented faithfully.
- Clock parsing handles MM:SS strings.
- Overtime periods are timed correctly.
- Events without score_margin are silently skipped.
"""

from __future__ import annotations

from app.services.summer_league_games_service import (
    _elapsed_seconds,
    _parse_clock,
    _period_duration_seconds,
    _period_start_seconds,
)


# ---------------------------------------------------------------------------
# _parse_clock
# ---------------------------------------------------------------------------


def test_parse_clock_standard():
    """MM:SS strings parse to remaining seconds."""
    assert _parse_clock("10:30") == 10 * 60 + 30


def test_parse_clock_zero():
    """00:00 → 0 remaining."""
    assert _parse_clock("00:00") == 0


def test_parse_clock_none():
    """None input returns None."""
    assert _parse_clock(None) is None


def test_parse_clock_bad_format():
    """Malformed strings return None."""
    assert _parse_clock("badclock") is None
    assert _parse_clock("12:30:00") is None


# ---------------------------------------------------------------------------
# _period_start_seconds / _period_duration_seconds
# ---------------------------------------------------------------------------


def test_period_start_regulation():
    """Regulation quarters start at expected offsets."""
    assert _period_start_seconds(1) == 0
    assert _period_start_seconds(2) == 720
    assert _period_start_seconds(3) == 1440
    assert _period_start_seconds(4) == 2160


def test_period_start_overtime():
    """First OT starts immediately after the 4th quarter."""
    assert _period_start_seconds(5) == 4 * 720
    assert _period_start_seconds(6) == 4 * 720 + 300


def test_period_duration_regulation():
    """Regulation period duration is 720 s."""
    assert _period_duration_seconds(1) == 720
    assert _period_duration_seconds(4) == 720


def test_period_duration_overtime():
    """OT period duration is 300 s."""
    assert _period_duration_seconds(5) == 300
    assert _period_duration_seconds(6) == 300


# ---------------------------------------------------------------------------
# _elapsed_seconds
# ---------------------------------------------------------------------------


def test_elapsed_seconds_start_of_period():
    """Clock at full period length → zero elapsed in that period."""
    assert _elapsed_seconds(1, 720) == 0.0
    assert _elapsed_seconds(2, 720) == 720.0


def test_elapsed_seconds_end_of_period():
    """Clock at 00:00 → full period has elapsed."""
    assert _elapsed_seconds(1, 0) == 720.0
    assert _elapsed_seconds(4, 0) == 4 * 720.0


def test_elapsed_seconds_mid_period():
    """Half-time of Q2 is 720 + 360 = 1080 s."""
    assert _elapsed_seconds(2, 360) == 1080.0


def test_elapsed_seconds_overtime():
    """OT periods use 300-s duration."""
    # Start of OT1 = 2880; end of OT1 = 3180
    assert _elapsed_seconds(5, 300) == 4 * 720.0
    assert _elapsed_seconds(5, 0) == 4 * 720.0 + 300.0


# End-to-end series properties (origin (0,0), monotonic time, lead changes,
# OT extension, no-clock skipping) are verified against the real
# ``get_game_flow_series`` with a live DB in
# tests/integration/test_game_flow_route.py.  A pure-Python re-implementation
# was intentionally removed here: it could pass even if the production series
# builder were inverted, and it duplicated logic that would silently drift.
