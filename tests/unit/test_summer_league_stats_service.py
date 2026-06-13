"""Unit tests for Summer League player-stats aggregation (DB-free).

Exercises the pure aggregation helper that powers the player-detail Summer
League section: per-game/per-36/per-100 transforms, weighted shooting %,
TS%, DNP exclusion, and graceful handling of missing pace / zero attempts.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.summer_league_stats_service import _aggregate_season


def _row(**kwargs: object) -> SimpleNamespace:
    """Build a minimal game-log row with sensible defaults."""
    base: dict[str, object] = dict(
        venue_slug="las_vegas",
        minutes_seconds=1800,
        starter_position=None,
        pace=100.0,
        pts=0,
        reb=0,
        ast=0,
        stl=0,
        blk=0,
        tov=0,
        fgm=0,
        fga=0,
        fg3m=0,
        fg3a=0,
        ftm=0,
        fta=0,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_per_game_and_per_36_and_per_100_math() -> None:
    """Two games aggregate into correct per-game, per-36, and per-100 lines."""
    rows = [
        _row(
            venue_slug="las_vegas",
            minutes_seconds=1800,  # 30.0 min
            starter_position="G",
            pace=100.0,
            pts=20,
            reb=10,
            ast=5,
            stl=2,
            blk=1,
            tov=3,
            fgm=8,
            fga=16,
            fg3m=2,
            fg3a=5,
            ftm=2,
            fta=2,
        ),
        _row(
            venue_slug="california_classic",
            minutes_seconds=1200,  # 20.0 min
            starter_position=None,
            pace=104.0,
            pts=10,
            reb=4,
            ast=3,
            stl=0,
            blk=0,
            tov=1,
            fgm=4,
            fga=10,
            fg3m=1,
            fg3a=4,
            ftm=1,
            fta=2,
        ),
    ]
    season = _aggregate_season(rows, year=2024, season_label="2024")
    assert season is not None

    assert season.gp == 2
    assert season.gs == 1
    assert season.total_minutes == pytest.approx(50.0)
    assert season.mpg == pytest.approx(25.0)
    assert season.venues == ["California Classic", "Las Vegas"]

    pg = season.modes["per_game"]
    assert pg.pts == pytest.approx(15.0)
    assert pg.reb == pytest.approx(7.0)
    assert pg.ast == pytest.approx(4.0)
    assert pg.blk == pytest.approx(0.5)

    p36 = season.modes["per_36"]
    # 30 pts over 50 min -> 30 * 36 / 50 = 21.6
    assert p36.pts == pytest.approx(21.6)
    assert p36.reb == pytest.approx(14 * 36 / 50)

    # possessions = 100*30/48 + 104*20/48 = 105.8333...
    expected_poss = 100.0 * 30 / 48 + 104.0 * 20 / 48
    assert season.total_possessions == pytest.approx(expected_poss)
    p100 = season.modes["per_100"]
    assert p100.pts == pytest.approx(30 * 100 / expected_poss)

    # Weighted shooting percentages (0-100 scale).
    assert season.fg_pct == pytest.approx(12 / 26 * 100)
    assert season.fg3_pct == pytest.approx(3 / 9 * 100)
    assert season.ft_pct == pytest.approx(75.0)
    assert season.fg3a_per_g == pytest.approx(4.5)
    assert season.fta_per_g == pytest.approx(2.0)
    # TS% = PTS / (2*(FGA + 0.44*FTA)) = 30 / (2*(26 + 1.76))
    assert season.ts_pct == pytest.approx(30 / (2 * (26 + 0.44 * 4)) * 100)


def test_dnp_rows_excluded_from_games_and_totals() -> None:
    """Rows with no minutes (DNP) do not count toward GP or stat sums."""
    rows = [
        _row(minutes_seconds=1800, pts=20, fga=10, fgm=5),
        _row(minutes_seconds=0, pts=99, fga=99, fgm=99),  # DNP, ignored
        _row(minutes_seconds=None, pts=99),  # DNP, ignored
    ]
    season = _aggregate_season(rows, year=2024, season_label="2024")
    assert season is not None
    assert season.gp == 1
    assert season.modes["per_game"].pts == pytest.approx(20.0)


def test_missing_pace_yields_no_per_100() -> None:
    """When no row has pace, per-100 is unavailable (None values)."""
    rows = [
        _row(minutes_seconds=1800, pace=None, pts=18, fga=12, fgm=6),
        _row(minutes_seconds=1200, pace=0, pts=10, fga=8, fgm=4),
    ]
    season = _aggregate_season(rows, year=2014, season_label="2014")
    assert season is not None
    assert season.total_possessions is None
    p100 = season.modes["per_100"]
    assert p100.pts is None and p100.reb is None
    # per-game / per-36 still work.
    assert season.modes["per_game"].pts == pytest.approx(14.0)


def test_zero_attempts_render_none_percentages() -> None:
    """No FGA/3PA/FTA -> percentages are None, not a divide-by-zero."""
    rows = [_row(minutes_seconds=600, pts=0, fga=0, fg3a=0, fta=0)]
    season = _aggregate_season(rows, year=2024, season_label="2024")
    assert season is not None
    assert season.fg_pct is None
    assert season.fg3_pct is None
    assert season.ft_pct is None
    assert season.ts_pct is None


def test_all_dnp_returns_none() -> None:
    """A span with only DNP rows aggregates to None (caller suppresses it)."""
    rows = [_row(minutes_seconds=0), _row(minutes_seconds=None)]
    assert _aggregate_season(rows, year=2024, season_label="2024") is None
