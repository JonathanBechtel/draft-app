"""Unit tests for the corrupt-minutes fallback in the box-score merge step.

stats.nba.com occasionally corrupts boxscoretraditionalv2 minutes for a single
game (observed on game 1322600006: starters' MIN ~+97:00, team MIN '705:00')
while boxscoreadvancedv2 carries the true values. The merge must fall back to
the advanced minutes when the traditional value is implausible, and to NULL
when no plausible source exists — never persist the corrupt number.
"""

from __future__ import annotations

from app.services.sources.summer_league.normalization import (
    MAX_PLAUSIBLE_PLAYER_SECONDS,
    MAX_PLAUSIBLE_TEAM_MINUTES,
    ParsedPlayerBoxRow,
    ParsedTeamBoxRow,
    _merge_player_box_rows,
    _merge_team_box_rows,
)


def _player_row(**kw: object) -> ParsedPlayerBoxRow:
    base: dict[str, object] = dict(
        game_id="1322600006",
        nba_stats_person_id="12345",
        raw_player_name="Cameron Carr",
        nba_stats_team_id="777",
    )
    base.update(kw)
    return ParsedPlayerBoxRow(**base)  # type: ignore[arg-type]


def _team_row(**kw: object) -> ParsedTeamBoxRow:
    base: dict[str, object] = dict(
        game_id="1322600006",
        nba_stats_team_id="777",
        raw_team_name="Spurs",
        raw_team_abbreviation="SAS",
    )
    base.update(kw)
    return ParsedTeamBoxRow(**base)  # type: ignore[arg-type]


def test_plausible_traditional_minutes_stay_authoritative() -> None:
    """Sane traditional minutes are kept even when advanced disagrees."""
    merged = _merge_player_box_rows(
        _player_row(minutes_seconds=1949),  # 32:29
        _player_row(minutes_seconds=1800),
        None,
    )
    assert merged.minutes_seconds == 1949


def test_corrupt_player_minutes_fall_back_to_advanced() -> None:
    """The observed corruption (129:53 traditional vs 32:29 advanced) repairs."""
    merged = _merge_player_box_rows(
        _player_row(minutes_seconds=7793),  # '129:53' — impossible
        _player_row(minutes_seconds=1949, usg_pct=0.226),
        None,
    )
    assert merged.minutes_seconds == 1949
    assert merged.usg_pct == 0.226  # advanced fields still merged


def test_corrupt_player_minutes_null_without_plausible_source() -> None:
    """No advanced row (or an equally corrupt one) nulls the value out."""
    no_advanced = _merge_player_box_rows(_player_row(minutes_seconds=7793), None, None)
    assert no_advanced.minutes_seconds is None

    both_corrupt = _merge_player_box_rows(
        _player_row(minutes_seconds=7793),
        _player_row(minutes_seconds=MAX_PLAUSIBLE_PLAYER_SECONDS + 1),
        None,
    )
    assert both_corrupt.minutes_seconds is None


def test_corrupt_team_minutes_fall_back_to_advanced() -> None:
    """Team MIN '705:00' repairs from the advanced box's 220:00."""
    merged = _merge_team_box_rows(
        _team_row(minutes=705),
        _team_row(minutes=220, pace=88.0),
    )
    assert merged.minutes == 220
    assert merged.pace == 88.0


def test_corrupt_team_minutes_null_without_plausible_source() -> None:
    """A corrupt team MIN with no advanced row nulls out instead of persisting."""
    merged = _merge_team_box_rows(_team_row(minutes=705), None)
    assert merged.minutes is None
    assert merged.minutes is None or merged.minutes <= MAX_PLAUSIBLE_TEAM_MINUTES


def test_sane_team_minutes_stay_authoritative() -> None:
    """Regulation team minutes (200) pass through untouched."""
    merged = _merge_team_box_rows(_team_row(minutes=200), _team_row(minutes=220))
    assert merged.minutes == 200
