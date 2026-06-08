"""Unit tests for Summer League raw-ingestion orchestration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from app.services.summer_league.raw_ingestion import (
    GAME_ENDPOINTS,
    RawIngestionOptions,
    SummerLeagueRawIngestor,
    extract_game_ids,
)
from app.services.summer_league.raw_store import SummerLeagueRawStore


class FakeNBAStatsClient:
    """Fake NBA Stats client for orchestration tests."""

    def __init__(
        self,
        *,
        failures: set[tuple[str, str | None]] | None = None,
    ) -> None:
        self.failures = failures or set()
        self.calls: list[tuple[str, dict[str, str]]] = []

    def fetch_json(self, endpoint: str, params: Mapping[str, str]) -> dict[str, object]:
        """Return deterministic fake payloads keyed by endpoint and params."""
        clean_params = dict(params)
        self.calls.append((endpoint, clean_params))
        game_id = clean_params.get("GameID")
        if (endpoint, game_id) in self.failures or (endpoint, None) in self.failures:
            raise RuntimeError(f"simulated {endpoint} failure")
        if endpoint == "leaguegamelog":
            if clean_params["PlayerOrTeam"] == "T":
                return _team_gamelog_payload()
            return _player_gamelog_payload()
        return _endpoint_payload(endpoint, game_id or "unknown")


def _team_gamelog_payload() -> dict[str, object]:
    return {
        "resultSets": [
            {
                "name": "LeagueGameLog",
                "headers": ["GAME_ID", "TEAM_ID", "PTS"],
                "rowSet": [
                    ["1522400002", 1, 91],
                    ["1522400002", 2, 89],
                    ["1522400001", 3, 100],
                    ["1522400001", 4, 97],
                ],
            }
        ]
    }


def _player_gamelog_payload() -> dict[str, object]:
    return {
        "resultSets": [
            {
                "name": "LeagueGameLog",
                "headers": ["GAME_ID", "PLAYER_ID"],
                "rowSet": [["1522400002", 1629639], ["1522400001", 1630173]],
            }
        ]
    }


def _endpoint_payload(endpoint: str, game_id: str) -> dict[str, object]:
    return {
        "resultSets": [
            {
                "name": endpoint,
                "headers": ["GAME_ID"],
                "rowSet": [[game_id]],
            }
        ]
    }


def test_extract_game_ids_deduplicates_in_gamelog_order() -> None:
    """Team gamelog rows include two teams per game; IDs are unique."""
    assert extract_game_ids(_team_gamelog_payload()) == ["1522400002", "1522400001"]


def test_extract_game_ids_returns_empty_when_header_missing() -> None:
    """Malformed payloads without GAME_ID do not crash ingestion."""
    payload = {
        "resultSets": [
            {"name": "LeagueGameLog", "headers": ["TEAM_ID"], "rowSet": [[1]]}
        ]
    }

    assert extract_game_ids(payload) == []


def test_fetch_year_league_writes_gamelogs_game_payloads_and_manifest(
    tmp_path: Path,
) -> None:
    """A normal run writes season gamelogs, each game endpoint, and manifest."""
    client = FakeNBAStatsClient()
    store = SummerLeagueRawStore(tmp_path)
    ingestor = SummerLeagueRawIngestor(client=client, store=store, sleep=lambda _: None)

    manifest = ingestor.fetch_year_league(RawIngestionOptions(year=2024, league_id="15"))

    assert manifest.team_gamelog_rows == 4
    assert manifest.player_gamelog_rows == 2
    assert manifest.game_ids == ["1522400002", "1522400001"]
    assert manifest.game_count == 2
    assert manifest.errors == []
    assert (tmp_path / "2024" / "15" / "leaguegamelog_team.json").exists()
    assert (tmp_path / "2024" / "15" / "leaguegamelog_player.json").exists()
    assert (
        tmp_path
        / "2024"
        / "15"
        / "games"
        / "1522400002"
        / "boxscoretraditionalv2.json"
    ).exists()
    manifest_payload = json.loads((tmp_path / "2024" / "15" / "manifest.json").read_text())
    assert manifest_payload["game_count"] == 2
    assert len([call for call in client.calls if call[0] == "leaguegamelog"]) == 2
    assert len([call for call in client.calls if call[0] in GAME_ENDPOINTS]) == 10


def test_fetch_year_league_limit_games_limits_per_game_fetches(tmp_path: Path) -> None:
    """limit_games narrows per-game detail calls without hiding discovered IDs."""
    client = FakeNBAStatsClient()
    ingestor = SummerLeagueRawIngestor(
        client=client,
        store=SummerLeagueRawStore(tmp_path),
        sleep=lambda _: None,
    )

    manifest = ingestor.fetch_year_league(
        RawIngestionOptions(year=2024, league_id="15", limit_games=1)
    )

    game_calls = [call for call in client.calls if call[0] in GAME_ENDPOINTS]
    assert manifest.game_ids == ["1522400002", "1522400001"]
    assert len(game_calls) == 5
    assert {params["GameID"] for _, params in game_calls} == {"1522400002"}


def test_fetch_year_league_skip_endpoints_omits_requested_details(
    tmp_path: Path,
) -> None:
    """Skipped endpoints are not requested or written for each game."""
    client = FakeNBAStatsClient()
    ingestor = SummerLeagueRawIngestor(
        client=client,
        store=SummerLeagueRawStore(tmp_path),
        sleep=lambda _: None,
    )

    ingestor.fetch_year_league(
        RawIngestionOptions(
            year=2024,
            league_id="15",
            limit_games=1,
            skip_endpoints=("playbyplayv2",),
        )
    )

    game_calls = [call for call in client.calls if call[0] in GAME_ENDPOINTS]
    assert len(game_calls) == 4
    assert "playbyplayv2" not in {endpoint for endpoint, _ in game_calls}
    assert not (
        tmp_path / "2024" / "15" / "games" / "1522400002" / "playbyplayv2.json"
    ).exists()


def test_fetch_year_league_dry_run_plans_without_per_game_fetches(
    tmp_path: Path,
) -> None:
    """Dry-run mode fetches gamelogs for IDs but only plans file writes."""
    client = FakeNBAStatsClient()
    store = SummerLeagueRawStore(tmp_path)
    ingestor = SummerLeagueRawIngestor(client=client, store=store, sleep=lambda _: None)

    manifest = ingestor.fetch_year_league(
        RawIngestionOptions(year=2024, league_id="15", dry_run=True, limit_games=1)
    )

    assert manifest.game_ids == ["1522400002", "1522400001"]
    assert len([call for call in client.calls if call[0] == "leaguegamelog"]) == 2
    assert [call for call in client.calls if call[0] in GAME_ENDPOINTS] == []
    assert not (tmp_path / "2024" / "15" / "leaguegamelog_team.json").exists()
    assert (tmp_path / "2024" / "15" / "manifest.json").exists()
    assert "2024/15/leaguegamelog_team.json" in manifest.files_skipped
    assert (
        "2024/15/games/1522400002/boxscoretraditionalv2.json"
        in manifest.files_skipped
    )


def test_fetch_year_league_records_partial_game_endpoint_failures(
    tmp_path: Path,
) -> None:
    """A failed endpoint is recorded while sibling endpoint writes continue."""
    client = FakeNBAStatsClient(failures={("playbyplayv2", "1522400002")})
    store = SummerLeagueRawStore(tmp_path)
    ingestor = SummerLeagueRawIngestor(client=client, store=store, sleep=lambda _: None)

    manifest = ingestor.fetch_year_league(
        RawIngestionOptions(year=2024, league_id="15", limit_games=1)
    )

    assert len(manifest.errors) == 1
    assert manifest.errors[0].endpoint == "playbyplayv2"
    assert manifest.errors[0].game_id == "1522400002"
    assert not (
        tmp_path / "2024" / "15" / "games" / "1522400002" / "playbyplayv2.json"
    ).exists()
    assert (
        tmp_path
        / "2024"
        / "15"
        / "games"
        / "1522400002"
        / "boxscorescoringv2.json"
    ).exists()


def test_fetch_year_league_uses_force_when_writing_existing_snapshots(
    tmp_path: Path,
) -> None:
    """Force mode overwrites existing raw files during refresh runs."""
    store = SummerLeagueRawStore(tmp_path)
    existing = store.season_file(year=2024, league_id="15", name="leaguegamelog_team")
    store.write_json(existing, {"old": True})
    ingestor = SummerLeagueRawIngestor(
        client=FakeNBAStatsClient(),
        store=store,
        sleep=lambda _: None,
    )

    ingestor.fetch_year_league(
        RawIngestionOptions(year=2024, league_id="15", limit_games=0, force=True)
    )

    payload = json.loads(existing.read_text())
    assert payload["resultSets"][0]["name"] == "LeagueGameLog"


def test_fetch_year_league_reuses_existing_snapshots_without_requests(
    tmp_path: Path,
) -> None:
    """Reruns reuse existing gamelogs and game payloads when force is false."""
    store = SummerLeagueRawStore(tmp_path)
    store.write_json(
        store.season_file(year=2024, league_id="15", name="leaguegamelog_team"),
        _team_gamelog_payload(),
    )
    store.write_json(
        store.season_file(year=2024, league_id="15", name="leaguegamelog_player"),
        _player_gamelog_payload(),
    )
    store.write_json(
        store.game_file(
            year=2024,
            league_id="15",
            game_id="1522400002",
            endpoint="boxscoretraditionalv2",
        ),
        _endpoint_payload("boxscoretraditionalv2", "1522400002"),
    )
    progress: list[str] = []
    client = FakeNBAStatsClient()
    ingestor = SummerLeagueRawIngestor(
        client=client,
        store=store,
        sleep=lambda _: None,
        progress=progress.append,
    )

    manifest = ingestor.fetch_year_league(
        RawIngestionOptions(year=2024, league_id="15", limit_games=1)
    )

    assert manifest.game_ids == ["1522400002", "1522400001"]
    assert ("boxscoretraditionalv2", {"GameID": "1522400002"}) not in client.calls
    assert any(message.startswith("reuse 2024/15/leaguegamelog_team") for message in progress)
    assert any(
        message.startswith("reuse 2024/15/games/1522400002/boxscoretraditionalv2")
        for message in progress
    )
