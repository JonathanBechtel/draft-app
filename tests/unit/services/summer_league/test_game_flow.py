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

import pytest

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


# ---------------------------------------------------------------------------
# End-to-end series properties (pure computation, no DB)
# ---------------------------------------------------------------------------


def _make_series(events: list[tuple[int, str, int]]) -> list[dict]:
    """Build a game-flow series from (period, clock, margin) tuples.

    Mirrors the logic in get_game_flow_series without a database.
    """
    from app.services.summer_league_games_service import (
        _elapsed_seconds,
        _parse_clock,
    )

    series: list[dict] = [{"t": 0.0, "margin": 0}]
    seen_t: float = 0.0
    for period, clock, margin in events:
        remaining = _parse_clock(clock)
        if remaining is None:
            continue
        t = _elapsed_seconds(period, remaining)
        if t < seen_t:
            t = seen_t
        seen_t = t
        series.append({"t": t, "margin": margin})
    return series


def test_series_monotonic_time():
    """Elapsed time t is non-decreasing across all points."""
    events = [
        (1, "10:00", 0),
        (1, "08:00", 2),
        (1, "06:00", -1),
        (2, "10:00", 3),
        (4, "00:00", 5),
    ]
    series = _make_series(events)
    times = [p["t"] for p in series]
    assert times == sorted(times), "times must be non-decreasing"


def test_series_endpoints_match_final_score():
    """Last point margin equals the final score margin."""
    events = [
        (1, "10:00", 0),
        (2, "05:00", 4),
        (4, "00:00", 7),  # final margin = home +7
    ]
    series = _make_series(events)
    assert series[0] == {"t": 0.0, "margin": 0}
    assert series[-1]["margin"] == 7


def test_series_origin_always_zero():
    """Series always starts at (0, 0) regardless of first event."""
    events = [(1, "11:30", 3)]
    series = _make_series(events)
    assert series[0] == {"t": 0.0, "margin": 0}


def test_series_lead_changes():
    """Lead changes produce correctly signed margin values."""
    events = [
        (1, "08:00", 2),   # home +2
        (1, "06:00", -1),  # away +1
        (1, "04:00", 3),   # home +3
    ]
    series = _make_series(events)
    margins = [p["margin"] for p in series]
    # Must include both positive and negative margins
    assert any(m > 0 for m in margins)
    assert any(m < 0 for m in margins)


def test_series_skips_events_without_clock():
    """None clock strings are silently skipped."""
    events = [
        (1, "10:00", 2),
        (1, None, 99),  # type: ignore[list-item]  # should be dropped
        (1, "08:00", 4),
    ]
    series = _make_series(events)  # type: ignore[arg-type]
    # Only 2 scored events + origin
    assert len(series) == 3
    assert series[-1]["margin"] == 4


def test_series_overtime_extends_beyond_regulation():
    """OT events produce elapsed times > 2880 s (4 × 720)."""
    events = [
        (4, "00:00", 0),  # tie at end of regulation
        (5, "04:00", 2),  # 1 min into OT1
        (5, "00:00", 3),  # end of OT1
    ]
    series = _make_series(events)
    ot_times = [p["t"] for p in series if p["t"] > 4 * 720]
    assert len(ot_times) >= 2, "OT events should extend past 2880 s"
