"""Unit tests for the Summer League scoreboard/schedule ingest (Job B step 0).

Covers the pure status-code mapping, UTC tip-time parsing, and payload
parsing/filtering -- no database, no network.

Several tests parse REAL captured ``scheduleleaguev2`` payloads rather than
hand-authored dicts (repo convention -- see ``test_summer_league_bracket.py``
for the same pattern against the same 2024 fixture):

* ``scheduleleaguev2_15_2024.json`` -- pre-existing repo fixture, real 2024
  Las Vegas Summer League final games (used elsewhere for bracket-round
  parsing).
* ``scheduleleaguev2_15_2026_live_pretip.json`` -- captured live from
  stats.nba.com (LeagueID 15, Season 2026) on 2026-07-10 ~19:34 UTC, a few
  hours into the real Las Vegas Summer League window: a genuine mix of real
  Final games (2026-07-09) and real not-yet-tipped Scheduled games spanning
  2026-07-10 through 2026-07-19.
* ``scoreboard_real_postponed_2021.json`` -- one real game (LeagueID 15,
  Season 2021, gameId 1522100005, WAS @ IND) trimmed from a live capture of
  that season's full schedule feed. This is the only real "PPD"
  (postponed) game found across every Summer League year/venue (2016-2026)
  this ingest step covers -- confirmed by an exhaustive capture sweep, not
  assumed.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

from app.schemas.summer_league import SummerLeagueGameStatus
from app.services.sources.summer_league.scoreboard_ingest import (
    _parse_game_date,
    _score_or_none,
    _team_id_or_none,
    _to_naive_utc,
    map_game_status,
    parse_scoreboard_games,
    parse_tip_datetime_utc,
)

_FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "summer_league"
_REAL_FINAL_FIXTURE = _FIXTURE_ROOT / "scheduleleaguev2_15_2024.json"
_REAL_LIVE_FIXTURE = _FIXTURE_ROOT / "scheduleleaguev2_15_2026_live_pretip.json"
_REAL_POSTPONED_FIXTURE = _FIXTURE_ROOT / "scoreboard_real_postponed_2021.json"


def _load_fixture(path: Path) -> dict[str, object]:
    return json.loads(path.read_text())  # type: ignore[no-any-return]


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


def test_map_game_status_postponed_text_is_postponed_not_live() -> None:
    """A real "PPD" status text maps to POSTPONED with its real, observed code.

    "PPD" is the real status text NBA Stats used for a rained-out SL game
    (LeagueID 15, 2021 season, gameId 1522100005). Fix #4: persisted as a
    real terminal POSTPONED status, not collapsed to SCHEDULED.
    """
    assert map_game_status(1, "PPD") == SummerLeagueGameStatus.POSTPONED


def test_map_game_status_postponed_text_beats_a_live_numeric_code() -> None:
    """Postponed/canceled text wins even when paired with a live/final code.

    Checked *before* the numeric code, so a feed that (however implausibly)
    still reports a live/final code alongside postponed/canceled text is
    never classified as live or final.
    """
    assert map_game_status(2, "PPD") == SummerLeagueGameStatus.POSTPONED
    assert map_game_status(3, "Postponed") == SummerLeagueGameStatus.POSTPONED
    assert map_game_status(2, "Canceled") == SummerLeagueGameStatus.CANCELED


def test_map_game_status_canceled_text_maps_to_canceled_not_postponed() -> None:
    """ "Cancel" text maps to the distinct CANCELED status, not POSTPONED."""
    assert map_game_status(1, "Game Canceled") == SummerLeagueGameStatus.CANCELED
    assert map_game_status(None, "cancelled") == SummerLeagueGameStatus.CANCELED


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


def test_to_naive_utc_passes_through_an_already_naive_datetime() -> None:
    """An already-naive datetime is returned unchanged (no tzinfo to strip)."""
    naive = datetime(2026, 7, 9, 22, 0)
    assert _to_naive_utc(naive) == naive


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


def test_team_id_or_none_stringifies_and_blanks_out_absent_values() -> None:
    """A provider teamId (int in the payload) is stringified; blanks are None."""
    assert _team_id_or_none(1610612739) == "1610612739"
    assert _team_id_or_none("1610612739") == "1610612739"
    assert _team_id_or_none(None) is None
    assert _team_id_or_none("") is None
    assert _team_id_or_none("  ") is None


def test_score_or_none_treats_zero_and_missing_as_not_yet_scored() -> None:
    """0 (the schedule feed's not-yet-tipped placeholder) and missing values are None."""
    assert _score_or_none(0) is None
    assert _score_or_none(None) is None
    assert _score_or_none(True) is None  # bool is an int subclass; not a real score.
    assert _score_or_none(79) == 79
    assert _score_or_none("34") == 34
    assert _score_or_none("not-a-number") is None


def _schedule_payload(*games_by_date: tuple[str, list[dict[str, object]]]) -> dict:
    return {
        "leagueSchedule": {
            "gameDates": [
                {"gameDate": game_date, "games": games}
                for game_date, games in games_by_date
            ]
        }
    }


def test_parse_scoreboard_games_filters_to_target_dates_when_given() -> None:
    """An explicit target_dates set still narrows the kept games (opt-in filter)."""
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


def test_parse_scoreboard_games_skips_a_game_with_no_resolvable_date() -> None:
    """A game with a real gameId but no usable date field anywhere is skipped.

    Applies even under the default full-horizon (target_dates=None) mode --
    a game with no resolvable date has no schedule-worthy identity to store.
    """
    payload = {
        "leagueSchedule": {
            "gameDates": [
                {
                    "gameDate": "not-a-real-date",
                    "games": [{"gameId": "no-date-game", "gameStatus": 1}],
                }
            ]
        }
    }
    assert parse_scoreboard_games(payload, target_dates=None) == []


def test_parse_scoreboard_games_empty_payload() -> None:
    """An empty or missing leagueSchedule yields no games."""
    assert parse_scoreboard_games({}, target_dates={date(2026, 7, 9)}) == []
    assert (
        parse_scoreboard_games(
            {"leagueSchedule": {"gameDates": []}}, target_dates={date(2026, 7, 9)}
        )
        == []
    )


def test_parse_scoreboard_games_extracts_real_team_ids_and_scores() -> None:
    """A real Final game's raw home/away provider team IDs and scores parse correctly.

    Fixture: 2024 Las Vegas Summer League, gameId 1522400001 (real payload) --
    Orlando Magic 106, home; Cleveland Cavaliers 79, away.
    """
    payload = _load_fixture(_REAL_FINAL_FIXTURE)
    games = parse_scoreboard_games(payload, target_dates=None)
    by_id = {g.nba_stats_game_id: g for g in games}

    game = by_id["1522400001"]
    assert game.status == SummerLeagueGameStatus.FINAL
    assert game.status_text == "Final"
    assert game.home_nba_stats_team_id == "1610612753"  # ORL
    assert game.away_nba_stats_team_id == "1610612739"  # CLE
    assert game.home_score == 106
    assert game.away_score == 79


def test_parse_scoreboard_games_default_keeps_the_full_real_schedule() -> None:
    """With no target_dates, every game across the real live capture's 11 dates is kept.

    Fixture: a live capture of the 2026 Las Vegas schedule feed spanning
    2026-07-09 (real Finals) through 2026-07-19 (real not-yet-tipped
    Scheduled games) -- 76 real games total.
    """
    payload = _load_fixture(_REAL_LIVE_FIXTURE)
    games = parse_scoreboard_games(payload, target_dates=None)

    assert len(games) == 76
    by_id = {g.nba_stats_game_id: g for g in games}
    # A real game tipping 07/12 -- more than two days past "today" (07/10 in
    # this live capture) -- is retained under the full-horizon default.
    assert "1522600024" in by_id
    assert by_id["1522600024"].game_date == date(2026, 7, 12)


def test_parse_scoreboard_games_explicit_target_dates_still_narrows_real_schedule() -> (
    None
):
    """Passing target_dates against the same real payload narrows to that window."""
    payload = _load_fixture(_REAL_LIVE_FIXTURE)
    games = parse_scoreboard_games(payload, target_dates={date(2026, 7, 10)})

    assert len(games) == 8
    assert all(g.game_date == date(2026, 7, 10) for g in games)
    assert "1522600024" not in {g.nba_stats_game_id for g in games}


def test_parse_scoreboard_games_scheduled_game_score_is_none_not_zero() -> None:
    """A real not-yet-tipped game's placeholder 0-0 score parses to None, not 0."""
    payload = _load_fixture(_REAL_LIVE_FIXTURE)
    games = parse_scoreboard_games(payload, target_dates={date(2026, 7, 10)})
    by_id = {g.nba_stats_game_id: g for g in games}

    game = by_id["1522600008"]
    assert game.status == SummerLeagueGameStatus.SCHEDULED
    assert game.home_score is None
    assert game.away_score is None
    assert game.home_nba_stats_team_id is not None
    assert game.away_nba_stats_team_id is not None


def test_parse_scoreboard_games_nonzero_score_overrides_stale_scheduled_code() -> None:
    """A scored game is live even when the schedule feed leaves status code 1 stale."""
    payload = {
        "leagueSchedule": {
            "gameDates": [
                {
                    "games": [
                        {
                            "gameId": "stale-live-1",
                            "gameStatus": 1,
                            "gameStatusText": "8:00 pm ET",
                            "gameDateTimeUTC": "2026-07-14T00:00:00Z",
                            "homeTeam": {"teamId": 1, "score": 51},
                            "awayTeam": {"teamId": 2, "score": 43},
                        }
                    ]
                }
            ]
        }
    }

    games = parse_scoreboard_games(payload)

    assert len(games) == 1
    assert games[0].status == SummerLeagueGameStatus.IN_PROGRESS
    assert games[0].home_score == 51
    assert games[0].away_score == 43


def test_parse_scoreboard_games_real_postponed_game_is_never_live() -> None:
    """The one real captured "PPD" game (2021 season) is parsed as POSTPONED.

    Fixture: LeagueID 15, Season 2021, gameId 1522100005 (WAS @ IND), rained
    out during Las Vegas Summer League -- the schedule feed still reports
    numeric code 1 (scheduled), but confirms the honest raw status text
    drives the real, persisted terminal POSTPONED status (fix #4) rather
    than the game being blurred with a merely-not-yet-tipped game.
    """
    payload = _load_fixture(_REAL_POSTPONED_FIXTURE)
    games = parse_scoreboard_games(payload, target_dates=None)

    assert len(games) == 1
    game = games[0]
    assert game.nba_stats_game_id == "1522100005"
    assert game.status == SummerLeagueGameStatus.POSTPONED
    assert game.status_text == "PPD"
    assert game.home_nba_stats_team_id == "1610612754"  # IND
    assert game.away_nba_stats_team_id == "1610612764"  # WAS
    assert game.home_score is None
    assert game.away_score is None
