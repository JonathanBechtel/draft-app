"""Unit tests for pure helpers in the Summer League games service."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.sources.summer_league.metrics import Box
from app.services.summer_league_games_service import (
    _box_line,
    _fg_str,
    _minutes,
    _pct,
    _venue_label,
)


def _team_box() -> Box:
    """A plausible full-game team box (240 player-minutes)."""
    return Box(
        mp=240.0,
        fgm=40,
        fga=85,
        fg3m=10,
        fg3a=30,
        ftm=10,
        fta=15,
        oreb=12,
        dreb=28,
        reb=40,
        ast=22,
        stl=8,
        blk=4,
        tov=14,
        pf=18,
        pts=100,
        gp=1,
    )


def _opp_box() -> Box:
    """A plausible opposing team box."""
    return Box(
        mp=240.0,
        fgm=36,
        fga=80,
        fg3m=8,
        fg3a=25,
        ftm=12,
        fta=18,
        oreb=10,
        dreb=30,
        reb=40,
        ast=20,
        stl=6,
        blk=3,
        tov=16,
        pf=20,
        pts=92,
        gp=1,
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


def _played_row(**overrides: object) -> SimpleNamespace:
    """A realistic played box row; keyword overrides tweak individual stats."""
    base: dict[str, object] = dict(
        player_id=1,
        slug="stub-guy",
        raw_player_name="Stub Guy",
        starter_position=None,
        minutes_seconds=1500,  # 25.0 minutes
        pts=18,
        oreb=2,
        dreb=4,
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
    base.update(overrides)
    return SimpleNamespace(**base)


def test_box_line_computes_single_game_advanced_rates() -> None:
    """A played row gets the full BBRef single-game advanced line.

    GmSc uses the full Hollinger weighting including OREB/DREB; AST% uses the
    BBRef single-game estimate against the team's minutes and FGM; the
    rebound/steal/block percentages and ORtg/DRtg use both team boxes.
    """
    tm, opp = _team_box(), _opp_box()
    line = _box_line(_played_row(), context=(tm, opp))
    # 18 +0.4*7 -0.7*12 -0.4*(2-2) +0.7*2 +0.3*4 +2 +0.7*4 +0.7*1 -0.4*2 -3
    assert line.gmsc == pytest.approx(16.7)
    assert line.fg3ar == pytest.approx(round(5 / 12, 3))
    assert line.ftr == pytest.approx(round(2 / 12, 3))
    assert line.tov_pct == pytest.approx(round(100 * 3 / (12 + 0.44 * 2 + 3), 1))
    # AST% = 100*4 / ((25/48)*40 - 7) ≈ 29.0
    assert line.ast_pct == pytest.approx(round(100 * 4 / ((25 / 48) * 40 - 7), 1))
    # ORB% = 100 * (2 * 48) / (25 * (12 + 30)) ≈ 9.1 — vs team+opponent boards.
    assert line.orb_pct == pytest.approx(round(100 * 2 * 48 / (25 * 42), 1))
    assert line.drb_pct == pytest.approx(round(100 * 4 * 48 / (25 * 38), 1))
    assert line.trb_pct == pytest.approx(round(100 * 6 * 48 / (25 * 80), 1))
    # BLK% denominator is opponent 2-point attempts (80 - 25 = 55).
    assert line.blk_pct == pytest.approx(round(100 * 1 * 48 / (25 * 55), 1))
    assert line.stl_pct is not None and line.stl_pct > 0
    # Dean Oliver ratings land in a plausible per-100 band.
    assert line.ortg is not None and 50 < line.ortg < 200
    assert line.drtg is not None and 50 < line.drtg < 200


def test_box_line_advanced_rates_guard_missing_context() -> None:
    """Opponent-relative rates degrade gracefully as context goes missing."""
    # Team box only: AST% computes, opponent-relative rates stay None.
    tm_only = _box_line(_played_row(), context=(_team_box(), None))
    assert tm_only.ast_pct is not None
    assert tm_only.orb_pct is None
    assert tm_only.ortg is None

    # No context at all: only the player-only rates compute.
    no_ctx = _box_line(_played_row())
    assert no_ctx.gmsc is not None
    assert no_ctx.ftr is not None
    assert no_ctx.ast_pct is None

    dnp = _box_line(_played_row(minutes_seconds=0), context=(_team_box(), _opp_box()))
    assert dnp.dnp is True
    assert dnp.gmsc is None
    assert dnp.ftr is None
    assert dnp.ast_pct is None
    assert dnp.tov_pct is None
    assert dnp.ortg is None
