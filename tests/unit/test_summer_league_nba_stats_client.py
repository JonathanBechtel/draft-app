"""Unit tests for Summer League NBA Stats client helpers."""

from __future__ import annotations

from typing import Any

import pytest

from app.services.summer_league.endpoints import (
    SUPPORTED_SUMMER_LEAGUES,
    build_boxscore_params,
    build_leaguegamelog_params,
    build_playbyplay_params,
    build_shotchart_params,
    normalize_league_id,
    normalize_season,
)
from app.services.summer_league.nba_stats_client import (
    NBA_API_ROOT,
    NBAStatsAPIError,
    NBAStatsClient,
    extract_result_sets,
    result_set_row_counts,
)


class FakeResponse:
    """Small response object for client tests."""

    def __init__(self, payload: object, *, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def json(self) -> object:
        """Return the configured payload or raise the configured exception."""
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeSession:
    """Fake curl_cffi-compatible session."""

    def __init__(
        self,
        response: FakeResponse | Exception | None = None,
        responses: list[FakeResponse | Exception] | None = None,
    ) -> None:
        self.response = response or FakeResponse({"resultSets": []})
        self.responses = responses or []
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.closed = False

    def get(self, url: str, params: dict[str, str]) -> FakeResponse:
        """Record the call and return the fake response."""
        self.calls.append((url, params))
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def close(self) -> None:
        """Mark the session as closed."""
        self.closed = True


class RaisingSession:
    """Fake session that raises transport errors."""

    def get(self, _url: str, params: dict[str, str]) -> FakeResponse:
        """Raise a simulated transport failure."""
        raise TimeoutError(f"timed out for {params}")


def test_supported_summer_league_mapping_matches_probe_findings() -> None:
    """The venue map uses the resolved NBA.com LeagueID assignments."""
    assert SUPPORTED_SUMMER_LEAGUES["15"].slug == "las_vegas"
    assert SUPPORTED_SUMMER_LEAGUES["13"].slug == "california_classic"
    assert SUPPORTED_SUMMER_LEAGUES["16"].slug == "salt_lake_city"
    assert SUPPORTED_SUMMER_LEAGUES["14"].slug == "orlando"


def test_normalize_league_id_accepts_supported_ids() -> None:
    """Supported Summer League IDs round-trip after trimming."""
    assert normalize_league_id(" 15 ") == "15"


def test_normalize_league_id_rejects_unsupported_ids() -> None:
    """Non-Summer-League IDs fail before a request is made."""
    with pytest.raises(ValueError, match="Unsupported Summer League"):
        normalize_league_id("00")


def test_normalize_season_requires_four_digit_year() -> None:
    """Summer League API calls use bare four-digit seasons."""
    assert normalize_season(2024) == "2024"
    assert normalize_season("2025") == "2025"
    with pytest.raises(ValueError, match="four-digit"):
        normalize_season("2024-25")


def test_build_leaguegamelog_params_includes_required_fields() -> None:
    """The leaguegamelog endpoint requires all core query params."""
    params = build_leaguegamelog_params(
        league_id="15", season=2024, player_or_team="T"
    )

    assert params == {
        "Counter": "1000",
        "Direction": "DESC",
        "LeagueID": "15",
        "PlayerOrTeam": "T",
        "Season": "2024",
        "SeasonType": "Regular Season",
        "Sorter": "DATE",
    }


def test_build_leaguegamelog_params_rejects_invalid_row_mode() -> None:
    """Only player and team gamelog modes are valid."""
    with pytest.raises(ValueError, match="player_or_team"):
        build_leaguegamelog_params(
            league_id="15",
            season=2024,
            player_or_team="X",  # type: ignore[arg-type]
        )


def test_build_boxscore_params_matches_probe_shape() -> None:
    """Boxscore endpoints share the same period/range params."""
    assert build_boxscore_params("1522400076") == {
        "GameID": "1522400076",
        "StartPeriod": "0",
        "EndPeriod": "10",
        "StartRange": "0",
        "EndRange": "28800",
        "RangeType": "0",
    }


def test_build_playbyplay_params_matches_probe_shape() -> None:
    """Play-by-play params include the game and full period range."""
    assert build_playbyplay_params("1522400076") == {
        "GameID": "1522400076",
        "StartPeriod": "0",
        "EndPeriod": "10",
    }


def test_build_shotchart_params_includes_required_filters() -> None:
    """Shot chart params constrain the query to game-level FGA rows."""
    params = build_shotchart_params(
        league_id="13",
        season="2024",
        game_id="1322400001",
    )

    assert params["LeagueID"] == "13"
    assert params["Season"] == "2024"
    assert params["GameID"] == "1322400001"
    assert params["ContextMeasure"] == "FGA"
    assert params["TeamID"] == "0"
    assert params["PlayerID"] == "0"
    assert params["SeasonType"] == "Regular Season"


def test_extract_result_sets_handles_result_sets_list() -> None:
    """NBA Stats list-shaped resultSets payloads are normalized."""
    payload: dict[str, Any] = {
        "resultSets": [
            {
                "name": "TeamStats",
                "headers": ["GAME_ID", "PTS"],
                "rowSet": [["1522400076", 91]],
            }
        ]
    }

    result_sets = extract_result_sets(payload)

    assert len(result_sets) == 1
    assert result_sets[0].name == "TeamStats"
    assert result_sets[0].headers == ["GAME_ID", "PTS"]
    assert result_sets[0].rows == [["1522400076", 91]]


def test_extract_result_sets_handles_single_dict_result_sets() -> None:
    """Some NBA Stats endpoints return a dict under resultSets."""
    payload: dict[str, Any] = {
        "resultSets": {
            "name": "PlayerStats",
            "headers": ["PLAYER_ID"],
            "rowSet": [[1629639]],
        }
    }

    result_sets = extract_result_sets(payload)

    assert len(result_sets) == 1
    assert result_sets[0].name == "PlayerStats"
    assert result_sets[0].rows == [[1629639]]


def test_extract_result_sets_handles_result_set_fallback() -> None:
    """Older/smaller NBA Stats responses may use resultSet."""
    payload: dict[str, Any] = {
        "resultSet": {
            "name": "Shots",
            "headers": ["GAME_ID"],
            "rowSet": [["1322400001"], ["1322400002"]],
        }
    }

    assert result_set_row_counts(payload) == {"Shots": 2}


def test_fetch_json_calls_endpoint_and_returns_payload() -> None:
    """The client joins endpoint names to the API root and returns JSON."""
    session = FakeSession(FakeResponse({"resultSets": []}))
    client = NBAStatsClient(session=session)

    payload = client.fetch_json("/leaguegamelog", {"LeagueID": "15"})

    assert payload == {"resultSets": []}
    assert session.calls == [
        (f"{NBA_API_ROOT}/leaguegamelog", {"LeagueID": "15"})
    ]


def test_fetch_json_raises_on_http_errors() -> None:
    """HTTP failures are wrapped in the client-specific exception."""
    client = NBAStatsClient(
        session=FakeSession(FakeResponse({}, status_code=500)),
        max_retries=0,
    )

    with pytest.raises(NBAStatsAPIError, match="HTTP 500"):
        client.fetch_json("leaguegamelog", {"LeagueID": "15"})


def test_fetch_json_raises_on_transport_errors() -> None:
    """Transport failures are wrapped in the client-specific exception."""
    client = NBAStatsClient(session=RaisingSession(), max_retries=0)

    with pytest.raises(NBAStatsAPIError, match="TimeoutError"):
        client.fetch_json("leaguegamelog", {"LeagueID": "15"})


def test_fetch_json_retries_transport_errors_then_returns_payload() -> None:
    """Transient transport failures are retried before surfacing."""
    sleeps: list[float] = []
    session = FakeSession(
        responses=[
            TimeoutError("temporary"),
            FakeResponse({"resultSets": [{"name": "Ok", "rowSet": []}]}),
        ]
    )
    client = NBAStatsClient(
        session=session,
        max_retries=2,
        retry_delay_seconds=0.5,
        sleep=sleeps.append,
    )

    payload = client.fetch_json("leaguegamelog", {"LeagueID": "15"})

    assert payload == {"resultSets": [{"name": "Ok", "rowSet": []}]}
    assert len(session.calls) == 2
    assert sleeps == [0.5]


def test_fetch_json_retries_retryable_http_statuses() -> None:
    """HTTP 5xx responses are retried when attempts remain."""
    session = FakeSession(
        responses=[
            FakeResponse({}, status_code=500),
            FakeResponse({"resultSets": []}, status_code=200),
        ]
    )
    client = NBAStatsClient(
        session=session,
        max_retries=1,
        retry_delay_seconds=0,
    )

    assert client.fetch_json("leaguegamelog", {"LeagueID": "15"}) == {"resultSets": []}
    assert len(session.calls) == 2


def test_fetch_json_does_not_retry_non_retryable_http_statuses() -> None:
    """HTTP 4xx responses other than rate limits fail immediately."""
    session = FakeSession(FakeResponse({}, status_code=404))
    client = NBAStatsClient(session=session, max_retries=2)

    with pytest.raises(NBAStatsAPIError, match="HTTP 404"):
        client.fetch_json("leaguegamelog", {"LeagueID": "15"})
    assert len(session.calls) == 1


def test_fetch_json_raises_on_non_json_response() -> None:
    """JSON decode failures are wrapped in the client-specific exception."""
    client = NBAStatsClient(session=FakeSession(FakeResponse(ValueError("bad json"))))

    with pytest.raises(NBAStatsAPIError, match="non-JSON"):
        client.fetch_json("leaguegamelog", {"LeagueID": "15"})


def test_fetch_json_raises_on_unexpected_json_shape() -> None:
    """NBA Stats payloads must decode to JSON objects."""
    client = NBAStatsClient(session=FakeSession(FakeResponse([])))

    with pytest.raises(NBAStatsAPIError, match="unexpected JSON shape"):
        client.fetch_json("leaguegamelog", {"LeagueID": "15"})


def test_context_manager_closes_owned_session() -> None:
    """Injected sessions are not owned or closed by the client."""
    session = FakeSession()
    with NBAStatsClient(session=session):
        pass

    assert session.closed is False
