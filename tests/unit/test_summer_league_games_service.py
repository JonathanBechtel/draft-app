"""Unit tests for pure helpers in the Summer League games service."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.summer_league_games_service import (
    _box_line,
    _fg_str,
    _minutes,
    _pct,
    _venue_label,
)


def test_venue_label_known_and_fallback() -> None:
    """Known slugs map to labels; unknown slugs are humanized; empty is em dash."""
    assert _venue_label("las_vegas") == "Las Vegas"
    assert _venue_label("salt_lake_city") == "Salt Lake City"
    assert _venue_label("some_new_venue") == "Some New Venue"
    assert _venue_label(None) == "—"


def test_minutes_converts_seconds_to_decimal() -> None:
    """Stored minutes-seconds convert to one-decimal minutes; falsy -> None."""
    assert _minutes(1800) == 30.0
    assert _minutes(1530) == 25.5
    assert _minutes(0) is None
    assert _minutes(None) is None


def test_pct_scales_fraction_and_preserves_none() -> None:
    """Fractions scale to a 0-100 percentage; None passes through."""
    assert _pct(0.5) == 50.0
    assert _pct(0.333) == 33.3
    assert _pct(None) is None


def test_fg_str_renders_made_attempted() -> None:
    """Made/attempted pairs render as 'm-a' with None coerced to 0."""
    assert _fg_str(4, 9) == "4-9"
    assert _fg_str(None, None) == "0-0"
    assert _fg_str(0, 3) == "0-3"


def test_box_line_marks_dnp_and_builds_shooting() -> None:
    """A zero-minute row is flagged DNP; shooting splits format correctly."""
    row = SimpleNamespace(
        player_id=7,
        slug="cooper-flagg",
        raw_player_name="Cooper Flagg",
        starter_position="F",
        minutes_seconds=0,
        pts=None,
        reb=None,
        ast=None,
        stl=None,
        blk=None,
        tov=None,
        pf=None,
        fgm=None,
        fga=None,
        fg3m=None,
        fg3a=None,
        ftm=None,
        fta=None,
        plus_minus=None,
        ts_pct=None,
        efg_pct=None,
        usg_pct=None,
    )
    line = _box_line(row)
    assert line.dnp is True
    assert line.starter is True
    assert line.slug == "cooper-flagg"
    assert line.fg == "0-0"


def test_box_line_played_row_carries_stats() -> None:
    """A played row is not DNP and surfaces counting + advanced stats."""
    row = SimpleNamespace(
        player_id=None,
        slug=None,
        raw_player_name="Stub Guy",
        starter_position=None,
        minutes_seconds=1500,
        pts=18,
        reb=6,
        ast=4,
        stl=2,
        blk=1,
        tov=3,
        pf=2,
        fgm=7,
        fga=12,
        fg3m=2,
        fg3a=5,
        ftm=2,
        fta=2,
        plus_minus=11,
        ts_pct=0.61,
        efg_pct=0.58,
        usg_pct=0.24,
    )
    line = _box_line(row)
    assert line.dnp is False
    assert line.starter is False
    assert line.minutes == pytest.approx(25.0)
    assert line.pts == 18
    assert line.fg == "7-12"
    assert line.fg3 == "2-5"
    assert line.ts_pct == pytest.approx(61.0)
    assert line.usg_pct == pytest.approx(24.0)
