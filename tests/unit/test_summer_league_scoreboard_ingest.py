"""Unit tests for the Summer League scoreboard/schedule ingest (Job B step 0).

Covers the pure status-code mapping and UTC tip-time parsing, plus payload
parsing/filtering -- no database, no network.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from app.schemas.summer_league import SummerLeagueGameStatus
from app.services.summer_league.scoreboard_ingest import (
    _parse_game_date,
    _to_naive_utc,
    map_game_status,
    parse_scoreboard_games,
    parse_tip_datetime_utc,
)


def test_map_game_status_scheduled_code() -> None:
    """Code 1 maps to SCHEDULED."""
    assert map_game_status(1, "7:00 pm ET") == SummerLeagueGameStatus.SCHEDULED


def test_map_game_status_in_progress_code() -> None:
    """Code 2 maps to IN_PROGRESS."""
    assert map_game_status(2, "Qtr 3 - 4:12") == SummerLeagueGameStatus.IN_PROGRESS


def test_map_game_status_final_code() -> None:
    """Code 3 maps to FINAL."""
    assert map_game_status(3, "Final") == SummerLeagueGameStatus.FINAL


def test_map_game_status_numeric_string_code() -> None:
    """A numeric-string code (as sometimes serialized) is coerced to int."""
    assert map_game_status("3", "Final") == SummerLeagueGameStatus.FINAL


def test_map_game_status_unknown_code_falls_back_to_text() -> None:
    """An unrecognized/missing code falls back to sniffing gameStatusText."""
    assert map_game_status(99, "Final/OT") == SummerLeagueGameStatus.FINAL
    assert map_game_status(None, "Halftime") == SummerLeagueGameStatus.IN_PROGRESS
    assert map_game_status(None, "Qtr 1 - 8:45") == SummerLeagueGameStatus.IN_PROGRESS


def test_map_game_status_defaults_to_unknown() -> None:
    """No usable code or text yields UNKNOWN."""
    assert map_game_status(None, None) == SummerLeagueGameStatus.UNKNOWN
    assert map_game_status(None, "") == SummerLeagueGameStatus.UNKNOWN


def test_map_game_status_treats_bool_as_not_a_code() -> None:
    """A stray bool (a Python int subclass) is never treated as a status code."""
    assert map_game_status(True, "Final") == SummerLeagueGameStatus.FINAL
    assert map_game_status(False, None) == SummerLeagueGameStatus.UNKNOWN


def test_parse_tip_datetime_utc_prefers_game_date_time_utc() -> None:
    """The gameDateTimeUTC field (correct date + time) is used when present."""
    game = {
        "gameDateTimeUTC": "2026-07-10T22:30:00Z",
        "gameDateUTC": "2026-07-10T00:00:00Z",
        "gameTimeUTC": "1900-01-01T01:00:00Z",
    }
    assert parse_tip_datetime_utc(game) == datetime(
        2026, 7, 10, 22, 30, tzinfo=timezone.utc
    )


def test_parse_tip_datetime_utc_combines_date_and_time_fallback() -> None:
    """Without gameDateTimeUTC, the real date + time-of-day are combined.

    gameTimeUTC on the NBA Stats schedule feed carries a placeholder
    1900-01-01 date -- only its time-of-day is meaningful.
    """
    game = {
        "gameDateUTC": "2026-07-11T00:00:00Z",
        "gameTimeUTC": "1900-01-01T20:00:00Z",
    }
    assert parse_tip_datetime_utc(game) == datetime(
        2026, 7, 11, 20, 0, tzinfo=timezone.utc
    )


def test_parse_tip_datetime_utc_returns_none_when_unparseable() -> None:
    """A game with no usable timestamp fields parses to None."""
    assert parse_tip_datetime_utc({}) is None
    assert parse_tip_datetime_utc({"gameDateTimeUTC": "not-a-date"}) is None


def test_parse_tip_datetime_utc_treats_naive_timestamps_as_utc() -> None:
    """A timestamp with no trailing Z/offset is assumed to already be UTC."""
    game = {"gameDateTimeUTC": "2026-07-09T22:00:00"}
    assert parse_tip_datetime_utc(game) == datetime(
        2026, 7, 9, 22, 0, tzinfo=timezone.utc
    )


def test_to_naive_utc_strips_tzinfo_and_passes_through_none() -> None:
    """Aware datetimes are converted to naive UTC; None passes through."""
    aware = datetime(2026, 7, 9, 22, 0, tzinfo=timezone.utc)
    assert _to_naive_utc(aware) == datetime(2026, 7, 9, 22, 0)
    assert _to_naive_utc(None) is None


def test_parse_game_date_falls_back_through_date_fields() -> None:
    """The gameDateEst field in ISO form (not the schedule's slash format) still parses."""
    assert _parse_game_date({"gameDateEst": "2026-07-09T00:00:00Z"}, None) == date(
        2026, 7, 9
    )


def test_parse_game_date_falls_back_to_tip_datetime_then_none() -> None:
    """With no date fields, the parsed tip time is used; otherwise None."""
    tip = datetime(2026, 7, 9, 22, 0, tzinfo=timezone.utc)
    assert _parse_game_date({}, tip) == date(2026, 7, 9)
    assert _parse_game_date({}, None) is None


def _schedule_payload(*games_by_date: tuple[str, list[dict[str, object]]]) -> dict:
    return {
        "leagueSchedule": {
            "gameDates": [
                {"gameDate": game_date, "games": games}
                for game_date, games in games_by_date
            ]
        }
    }


def test_parse_scoreboard_games_filters_to_target_dates() -> None:
    """Only games whose schedule date is in target_dates are kept."""
    payload = _schedule_payload(
        (
            "07/09/2026 00:00:00",
            [
                {
                    "gameId": "today-1",
                    "gameStatus": 2,
                    "gameStatusText": "Qtr 2 - 5:00",
                    "gameDateTimeUTC": "2026-07-09T22:00:00Z",
                }
            ],
        ),
        (
            "07/10/2026 00:00:00",
            [
                {
                    "gameId": "tomorrow-1",
                    "gameStatus": 1,
                    "gameStatusText": "7:00 pm ET",
                    "gameDateTimeUTC": "2026-07-10T23:00:00Z",
                }
            ],
        ),
        (
            "07/12/2026 00:00:00",
            [
                {
                    "gameId": "out-of-window",
                    "gameStatus": 1,
                    "gameDateTimeUTC": "2026-07-12T23:00:00Z",
                }
            ],
        ),
    )

    games = parse_scoreboard_games(
        payload, target_dates={date(2026, 7, 9), date(2026, 7, 10)}
    )

    assert [g.nba_stats_game_id for g in games] == ["today-1", "tomorrow-1"]
    assert games[0].status == SummerLeagueGameStatus.IN_PROGRESS
    assert games[0].tip_datetime == datetime(2026, 7, 9, 22, 0, tzinfo=timezone.utc)
    assert games[0].game_date == date(2026, 7, 9)
    assert games[1].status == SummerLeagueGameStatus.SCHEDULED


def test_parse_scoreboard_games_skips_games_without_an_id() -> None:
    """A malformed game row with no gameId is skipped rather than erroring."""
    payload = _schedule_payload(
        ("07/09/2026 00:00:00", [{"gameStatus": 1}]),
    )
    assert parse_scoreboard_games(payload, target_dates={date(2026, 7, 9)}) == []


def test_parse_scoreboard_games_empty_payload() -> None:
    """An empty or missing leagueSchedule yields no games."""
    assert parse_scoreboard_games({}, target_dates={date(2026, 7, 9)}) == []
    assert (
        parse_scoreboard_games(
            {"leagueSchedule": {"gameDates": []}}, target_dates={date(2026, 7, 9)}
        )
        == []
    )
