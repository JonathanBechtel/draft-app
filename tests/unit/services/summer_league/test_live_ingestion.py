"""Unit tests for targeted live raw refresh orchestration (ticket #531).

Covers the DB-free pieces: clock/naive-UTC helpers, grouping selected games
by (year, LeagueID), and :func:`refresh_selected_games`'s call-shape against
a recording fake NBA Stats client/store, mirroring the fake-client
convention in ``tests/unit/test_summer_league_raw_ingestion.py``. The
DB-selection half
(:func:`~app.services.summer_league.live_ingestion.select_active_window_games`)
is covered by an integration test seeded from a real captured schedule
payload -- see ``tests/integration/test_summer_league_live_ingestion.py``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

import pytest

from app.services.summer_league.live_ingestion import (
    LiveGameSelection,
    _default_clock,
    _naive_utc,
    group_by_year_league,
    refresh_selected_games,
)
from app.services.summer_league.raw_ingestion import (
    GAME_ENDPOINTS,
    REQUIRED_GAME_ENDPOINTS,
)
from app.services.summer_league.raw_store import SummerLeagueRawStore


def test_default_clock_returns_naive_utc_now() -> None:
    """The default clock returns a naive datetime, matching the schema's column convention."""
    now = _default_clock()
    assert now.tzinfo is None


def test_naive_utc_converts_an_aware_datetime_to_naive_utc() -> None:
    """An aware datetime is converted to UTC and stripped of tzinfo."""
    aware = datetime(2026, 7, 10, 19, 30, tzinfo=timezone.utc)
    assert _naive_utc(aware) == datetime(2026, 7, 10, 19, 30)


def test_naive_utc_passes_an_already_naive_datetime_through_unchanged() -> None:
    """An already-naive datetime is returned unchanged (no tzinfo to strip)."""
    naive = datetime(2026, 7, 10, 19, 30)
    assert _naive_utc(naive) == naive


class FakeNBAStatsClient:
    """Recording fake NBA Stats client -- never touches the network.

    Failure keys are ``(endpoint, LeagueID, PlayerOrTeam)`` for
    ``leaguegamelog`` calls (which carry no ``GameID``) and
    ``(endpoint, GameID)`` for every game-scoped endpoint -- distinct enough
    to fail one (year, LeagueID) group's season gamelog without also
    matching a sibling group's identical ``PlayerOrTeam`` value.
    """

    def __init__(self, *, failures: set[tuple[str, ...]] | None = None) -> None:
        self.failures = failures or set()
        self.calls: list[tuple[str, dict[str, str]]] = []

    def fetch_json(self, endpoint: str, params: Mapping[str, str]) -> dict[str, object]:
        """Record the call and return a deterministic fake payload."""
        clean_params = dict(params)
        self.calls.append((endpoint, clean_params))
        if endpoint == "leaguegamelog":
            failure_key: tuple[str, ...] = (
                endpoint,
                clean_params.get("LeagueID", ""),
                clean_params.get("PlayerOrTeam", ""),
            )
        else:
            failure_key = (endpoint, clean_params.get("GameID", ""))
        if failure_key in self.failures:
            raise RuntimeError(f"simulated {endpoint} failure")
        if endpoint == "leaguegamelog":
            player_or_team = clean_params["PlayerOrTeam"]
            rows = (
                [["live-1", 1, 91], ["live-2", 2, 89]]
                if player_or_team == "T"
                else [["live-1", 1629639], ["live-2", 1630173]]
            )
            return {
                "resultSets": [
                    {"name": "LeagueGameLog", "headers": ["GAME_ID"], "rowSet": rows}
                ]
            }
        return {
            "resultSets": [
                {
                    "name": endpoint,
                    "headers": ["GAME_ID"],
                    "rowSet": [[clean_params.get("GameID") or "unknown"]],
                }
            ]
        }


def test_group_by_year_league_groups_and_preserves_order() -> None:
    """Selections split by (year, league_id), keeping each group's input order."""
    selections = [
        LiveGameSelection(nba_stats_game_id="a", year=2026, league_id="15"),
        LiveGameSelection(nba_stats_game_id="b", year=2026, league_id="13"),
        LiveGameSelection(nba_stats_game_id="c", year=2026, league_id="15"),
    ]

    grouped = group_by_year_league(selections)

    assert grouped == {
        (2026, "15"): ["a", "c"],
        (2026, "13"): ["b"],
    }


def test_group_by_year_league_empty_returns_empty_dict() -> None:
    """No selections groups to an empty mapping."""
    assert group_by_year_league([]) == {}


def test_refresh_selected_games_empty_selection_makes_no_calls(tmp_path: Path) -> None:
    """Empty selection makes zero per-game (and zero group) network calls."""
    client = FakeNBAStatsClient()

    report = refresh_selected_games(
        [], client=client, store=SummerLeagueRawStore(tmp_path), sleep=lambda _: None
    )

    assert client.calls == []
    assert report.selected == 0
    assert report.groups == 0
    assert report.written == 0
    assert report.errors == 0


def test_refresh_selected_games_calls_only_selected_ids_and_replaces_existing(
    tmp_path: Path,
) -> None:
    """Two selected game IDs produce calls only for those IDs and replace existing files."""
    store = SummerLeagueRawStore(tmp_path)
    stale_path = store.game_file(
        year=2026, league_id="15", game_id="live-1", endpoint="boxscoretraditionalv2"
    )
    store.write_json(stale_path, {"stale": True})
    client = FakeNBAStatsClient()

    selections = [
        LiveGameSelection(nba_stats_game_id="live-1", year=2026, league_id="15"),
        LiveGameSelection(nba_stats_game_id="live-2", year=2026, league_id="15"),
    ]
    report = refresh_selected_games(
        selections, client=client, store=store, sleep=lambda _: None
    )

    game_calls = [call for call in client.calls if call[0] in GAME_ENDPOINTS]
    assert {params["GameID"] for _, params in game_calls} == {"live-1", "live-2"}
    assert len(game_calls) == 2 * len(GAME_ENDPOINTS)
    assert json.loads(stale_path.read_text()) != {"stale": True}
    assert report.selected == 2
    assert report.groups == 1
    assert report.errors == 0
    assert report.written > 0


def test_refresh_selected_games_splits_calls_by_year_league_group(
    tmp_path: Path,
) -> None:
    """Selections spanning two (year, league_id) groups issue one ingestion run each."""
    client = FakeNBAStatsClient()
    selections = [
        LiveGameSelection(nba_stats_game_id="live-1", year=2026, league_id="15"),
        LiveGameSelection(nba_stats_game_id="live-2", year=2026, league_id="13"),
    ]

    report = refresh_selected_games(
        selections, client=client, store=SummerLeagueRawStore(tmp_path), sleep=lambda _: None
    )

    leaguegamelog_calls = [
        params for endpoint, params in client.calls if endpoint == "leaguegamelog"
    ]
    called_league_ids = {params["LeagueID"] for params in leaguegamelog_calls}
    assert called_league_ids == {"15", "13"}
    assert report.groups == 2
    assert report.selected == 2


def test_refresh_selected_games_group_gamelog_failure_is_an_error_not_a_success(
    tmp_path: Path,
) -> None:
    """A group whose required season gamelog fails is recorded as an error.

    Confirms errors are visible and cannot be reported as successful
    freshness -- and that a failed group does not abort a sibling group.
    """
    client = FakeNBAStatsClient(failures={("leaguegamelog", "15", "T")})
    selections = [
        LiveGameSelection(nba_stats_game_id="live-1", year=2026, league_id="15"),
        LiveGameSelection(nba_stats_game_id="live-2", year=2026, league_id="13"),
    ]

    report = refresh_selected_games(
        selections, client=client, store=SummerLeagueRawStore(tmp_path), sleep=lambda _: None
    )

    assert report.errors == 1
    assert any("2026/15" in message for message in report.error_messages)
    # The sibling group (LeagueID 13) still completed successfully.
    game_calls = [call for call in client.calls if call[0] in GAME_ENDPOINTS]
    assert {params["GameID"] for _, params in game_calls} == {"live-2"}
    assert report.written > 0


def test_refresh_selected_games_per_endpoint_failure_counts_as_error_not_written(
    tmp_path: Path,
) -> None:
    """A per-game endpoint failure is counted as an error, never as a written file."""
    client = FakeNBAStatsClient(failures={("playbyplayv2", "live-1")})
    selections = [LiveGameSelection(nba_stats_game_id="live-1", year=2026, league_id="15")]

    report = refresh_selected_games(
        selections, client=client, store=SummerLeagueRawStore(tmp_path), sleep=lambda _: None
    )

    assert report.errors == 1
    assert any("playbyplayv2" in message for message in report.error_messages)
    # Sibling endpoints for the same game still succeed.
    assert report.written == len(GAME_ENDPOINTS) - 1 + 2  # game endpoints + 2 gamelogs
    # playbyplayv2 is non-blocking: an optional-endpoint hiccup must never
    # count toward required_errors (that would abort the whole tick, #530's
    # gate) -- only the box-score endpoints do.
    assert report.required_errors == 0


@pytest.mark.parametrize(
    "endpoint", sorted(REQUIRED_GAME_ENDPOINTS)
)
def test_refresh_selected_games_critical_box_score_failure_increments_required_errors(
    tmp_path: Path, endpoint: str
) -> None:
    """A failed traditional/advanced/scoring box-score fetch blocks freshness.

    These three endpoints are (or directly feed) the player line the Desk
    renders; `force=True` leaves the OLD on-disk snapshot in place on a
    fetch failure, so a failure here must count toward `required_errors` so
    the tick (#530's gate in `app/cli/sl_desk_tick.py`) aborts before
    stamping fresh state on a stale line.
    """
    client = FakeNBAStatsClient(failures={(endpoint, "live-1")})
    selections = [LiveGameSelection(nba_stats_game_id="live-1", year=2026, league_id="15")]

    report = refresh_selected_games(
        selections, client=client, store=SummerLeagueRawStore(tmp_path), sleep=lambda _: None
    )

    assert report.errors == 1
    assert report.required_errors == 1
    assert any(endpoint in message for message in report.error_messages)


@pytest.mark.parametrize("endpoint", ["playbyplayv2", "shotchartdetail"])
def test_refresh_selected_games_non_critical_endpoint_failure_stays_optional(
    tmp_path: Path, endpoint: str
) -> None:
    """A failed play-by-play/shot-chart fetch is a visible error but never blocks freshness.

    Unlike the box-score endpoints, these do not feed the player line the
    Desk renders, so a bad fetch here must land in `errors` only --
    `required_errors` must stay 0 so #530's tick-abort gate does not fire.
    """
    assert endpoint not in REQUIRED_GAME_ENDPOINTS
    client = FakeNBAStatsClient(failures={(endpoint, "live-1")})
    selections = [LiveGameSelection(nba_stats_game_id="live-1", year=2026, league_id="15")]

    report = refresh_selected_games(
        selections, client=client, store=SummerLeagueRawStore(tmp_path), sleep=lambda _: None
    )

    assert report.errors == 1
    assert report.required_errors == 0


def test_refresh_selected_games_mixed_critical_and_optional_failures_counts_only_critical(
    tmp_path: Path,
) -> None:
    """Two failures in one refresh -- only the critical one counts toward required_errors."""
    client = FakeNBAStatsClient(
        failures={
            ("boxscoretraditionalv2", "live-1"),
            ("shotchartdetail", "live-1"),
        }
    )
    selections = [LiveGameSelection(nba_stats_game_id="live-1", year=2026, league_id="15")]

    report = refresh_selected_games(
        selections, client=client, store=SummerLeagueRawStore(tmp_path), sleep=lambda _: None
    )

    assert report.errors == 2
    assert report.required_errors == 1
