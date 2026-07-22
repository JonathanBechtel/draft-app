"""Unit tests for Summer League raw-ingestion orchestration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

import pytest

from app.services.summer_league.manifest import SummerLeagueRawManifest
from app.services.summer_league.raw_ingestion import (
    GAME_ENDPOINTS,
    REQUIRED_GAME_ENDPOINTS,
    RawIngestionOptions,
    SummerLeagueRequiredGamelogError,
    SummerLeagueRawIngestor,
    dirty_game_ids_from_manifest,
    extract_game_ids,
    is_required_game_endpoint,
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
        failure_keys = {
            (endpoint, game_id),
            (endpoint, None),
        }
        if endpoint == "leaguegamelog":
            failure_keys.add((endpoint, clean_params.get("PlayerOrTeam")))
        if self.failures.intersection(failure_keys):
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


def test_required_game_endpoints_are_exactly_the_box_score_endpoints() -> None:
    """The critical set is the three box-score endpoints, nothing else.

    Guards the single source of truth `live_ingestion.refresh_selected_games`
    reads to decide `LiveIngestionReport.required_errors` -- pbp/shotchart
    must stay outside it (non-blocking), while every box-score endpoint must
    be inside it (blocks freshness).
    """
    assert REQUIRED_GAME_ENDPOINTS == {
        "boxscoretraditionalv2",
        "boxscoreadvancedv2",
        "boxscorescoringv2",
    }
    assert REQUIRED_GAME_ENDPOINTS < set(GAME_ENDPOINTS)


@pytest.mark.parametrize("endpoint", sorted(REQUIRED_GAME_ENDPOINTS))
def test_is_required_game_endpoint_true_for_box_score_endpoints(endpoint: str) -> None:
    """Every box-score endpoint classifies as required."""
    assert is_required_game_endpoint(endpoint) is True


@pytest.mark.parametrize("endpoint", ["playbyplayv2", "shotchartdetail", "unknownendpoint"])
def test_is_required_game_endpoint_false_for_non_box_score_endpoints(endpoint: str) -> None:
    """pbp/shotchart (and anything unrecognized) classify as non-blocking."""
    assert is_required_game_endpoint(endpoint) is False


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


def test_fetch_year_league_fails_when_required_team_gamelog_fails(
    tmp_path: Path,
) -> None:
    """A missing team gamelog fails the run because it indexes game IDs."""
    client = FakeNBAStatsClient(failures={("leaguegamelog", "T")})
    store = SummerLeagueRawStore(tmp_path)
    ingestor = SummerLeagueRawIngestor(client=client, store=store, sleep=lambda _: None)

    with pytest.raises(
        SummerLeagueRequiredGamelogError,
        match=r"Required team leaguegamelog failed for 2024/15",
    ):
        ingestor.fetch_year_league(RawIngestionOptions(year=2024, league_id="15"))

    manifest_payload = json.loads((tmp_path / "2024" / "15" / "manifest.json").read_text())
    assert manifest_payload["game_count"] == 0
    assert manifest_payload["team_gamelog_rows"] == 0
    assert manifest_payload["errors"][0]["endpoint"] == "leaguegamelog"
    assert [endpoint for endpoint, _ in client.calls] == ["leaguegamelog"]


def test_fetch_year_league_fails_when_required_player_gamelog_fails(
    tmp_path: Path,
) -> None:
    """A missing player gamelog fails before optional per-game endpoints run."""
    client = FakeNBAStatsClient(failures={("leaguegamelog", "P")})
    store = SummerLeagueRawStore(tmp_path)
    ingestor = SummerLeagueRawIngestor(client=client, store=store, sleep=lambda _: None)

    with pytest.raises(
        SummerLeagueRequiredGamelogError,
        match=r"Required player leaguegamelog failed for 2024/15",
    ):
        ingestor.fetch_year_league(RawIngestionOptions(year=2024, league_id="15"))

    manifest_payload = json.loads((tmp_path / "2024" / "15" / "manifest.json").read_text())
    assert manifest_payload["game_count"] == 2
    assert manifest_payload["team_gamelog_rows"] == 4
    assert manifest_payload["player_gamelog_rows"] == 0
    assert manifest_payload["errors"][0]["endpoint"] == "leaguegamelog"
    assert [endpoint for endpoint, _ in client.calls] == [
        "leaguegamelog",
        "leaguegamelog",
    ]


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


def test_fetch_year_league_game_ids_fetches_only_those_exact_ids(
    tmp_path: Path,
) -> None:
    """Explicit game_ids scopes per-game endpoint calls to exactly those IDs.

    Ticket #531: a targeted live refresh names specific game IDs directly,
    bypassing the season gamelog's discovered list entirely.
    """
    client = FakeNBAStatsClient()
    store = SummerLeagueRawStore(tmp_path)
    ingestor = SummerLeagueRawIngestor(client=client, store=store, sleep=lambda _: None)

    manifest = ingestor.fetch_year_league(
        RawIngestionOptions(
            year=2024, league_id="15", game_ids=("1522400001", "9999999999")
        )
    )

    game_calls = [call for call in client.calls if call[0] in GAME_ENDPOINTS]
    assert {params["GameID"] for _, params in game_calls} == {
        "1522400001",
        "9999999999",
    }
    assert len(game_calls) == 2 * len(GAME_ENDPOINTS)
    # The season gamelog's own discovered list is untouched -- game_ids only
    # scopes which games get per-game endpoint calls, not manifest metadata.
    assert manifest.game_ids == ["1522400002", "1522400001"]


def test_fetch_year_league_game_ids_empty_tuple_fetches_no_game_endpoints(
    tmp_path: Path,
) -> None:
    """An empty game_ids tuple makes zero per-game network calls."""
    client = FakeNBAStatsClient()
    store = SummerLeagueRawStore(tmp_path)
    ingestor = SummerLeagueRawIngestor(client=client, store=store, sleep=lambda _: None)

    manifest = ingestor.fetch_year_league(
        RawIngestionOptions(year=2024, league_id="15", game_ids=())
    )

    assert [call for call in client.calls if call[0] in GAME_ENDPOINTS] == []
    assert manifest.files_written == [
        "2024/15/leaguegamelog_team.json",
        "2024/15/leaguegamelog_player.json",
    ]


def test_fetch_year_league_game_ids_none_preserves_existing_behavior(
    tmp_path: Path,
) -> None:
    """game_ids=None (the default) is identical to the pre-#531 discovery path."""
    client = FakeNBAStatsClient()
    store = SummerLeagueRawStore(tmp_path)
    ingestor = SummerLeagueRawIngestor(client=client, store=store, sleep=lambda _: None)

    manifest = ingestor.fetch_year_league(
        RawIngestionOptions(year=2024, league_id="15", limit_games=1)
    )

    game_calls = [call for call in client.calls if call[0] in GAME_ENDPOINTS]
    assert {params["GameID"] for _, params in game_calls} == {"1522400002"}
    assert manifest.game_ids == ["1522400002", "1522400001"]


def test_fetch_year_league_game_ids_force_replaces_existing_files(
    tmp_path: Path,
) -> None:
    """game_ids + force=True overwrites an existing snapshot for a named game.

    This is the exact shape a live refresh needs: two selected game IDs
    produce calls only for those IDs, and replace whatever was already on
    disk rather than reusing a stale immutable-looking snapshot.
    """
    store = SummerLeagueRawStore(tmp_path)
    stale_path = store.game_file(
        year=2024, league_id="15", game_id="1522400001", endpoint="boxscoretraditionalv2"
    )
    store.write_json(stale_path, {"stale": True})
    other_path = store.game_file(
        year=2024, league_id="15", game_id="9999999999", endpoint="boxscoretraditionalv2"
    )
    assert not other_path.exists()

    client = FakeNBAStatsClient()
    ingestor = SummerLeagueRawIngestor(client=client, store=store, sleep=lambda _: None)

    manifest = ingestor.fetch_year_league(
        RawIngestionOptions(
            year=2024,
            league_id="15",
            game_ids=("1522400001", "9999999999"),
            force=True,
        )
    )

    game_calls = [call for call in client.calls if call[0] in GAME_ENDPOINTS]
    assert {params["GameID"] for _, params in game_calls} == {
        "1522400001",
        "9999999999",
    }
    refreshed = json.loads(stale_path.read_text())
    assert refreshed != {"stale": True}
    assert other_path.exists()
    assert manifest.errors == []


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


# ---------------------------------------------------------------------------
# dirty_game_ids_from_manifest
# ---------------------------------------------------------------------------


def _manifest_with_files(
    *, files_written: list[str], files_skipped: list[str] | None = None
) -> SummerLeagueRawManifest:
    manifest = SummerLeagueRawManifest.start(year=2024, league_id="15")
    manifest.files_written = list(files_written)
    manifest.files_skipped = list(files_skipped or [])
    return manifest


def test_dirty_game_ids_from_manifest_mixed_written_and_skipped() -> None:
    """Only per-game files that were actually written mark a game dirty.

    A three-game manifest: game 001 is brand new (every endpoint written),
    game 002 had one file force-rewritten (boxscoretraditionalv2) while its
    other endpoints were reused (skipped), and game 003 is fully unchanged
    (every endpoint skipped). Season-level files (leaguegamelog_team/player)
    are present in files_written too and must never be mistaken for a game.
    """
    manifest = _manifest_with_files(
        files_written=[
            "2024/15/leaguegamelog_team.json",
            "2024/15/leaguegamelog_player.json",
            "2024/15/games/001/boxscoretraditionalv2.json",
            "2024/15/games/001/boxscoreadvancedv2.json",
            "2024/15/games/001/boxscorescoringv2.json",
            "2024/15/games/001/playbyplayv2.json",
            "2024/15/games/001/shotchartdetail.json",
            "2024/15/games/002/boxscoretraditionalv2.json",
        ],
        files_skipped=[
            "2024/15/games/002/boxscoreadvancedv2.json",
            "2024/15/games/002/boxscorescoringv2.json",
            "2024/15/games/002/playbyplayv2.json",
            "2024/15/games/002/shotchartdetail.json",
            "2024/15/games/003/boxscoretraditionalv2.json",
            "2024/15/games/003/boxscoreadvancedv2.json",
            "2024/15/games/003/boxscorescoringv2.json",
            "2024/15/games/003/playbyplayv2.json",
            "2024/15/games/003/shotchartdetail.json",
        ],
    )

    assert dirty_game_ids_from_manifest(manifest) == {"001", "002"}


def test_dirty_game_ids_from_manifest_no_writes_is_empty() -> None:
    """A fully-reused venue (nothing written) has no dirty games."""
    manifest = _manifest_with_files(
        files_written=[],
        files_skipped=["2024/15/games/001/boxscoretraditionalv2.json"],
    )
    assert dirty_game_ids_from_manifest(manifest) == set()


def test_dirty_game_ids_from_manifest_scoped_to_endpoints() -> None:
    """The ``endpoints`` filter narrows detection to specific per-game files.

    Mirrors how the ingest runner distinguishes "shot-relevant" from
    "PBP-relevant" dirtiness so a box-score-only rewrite never invalidates
    SHOT/PBP batch progress for a game whose shot/PBP files were untouched.
    """
    manifest = _manifest_with_files(
        files_written=[
            "2024/15/games/001/boxscoretraditionalv2.json",
            "2024/15/games/002/shotchartdetail.json",
            "2024/15/games/003/playbyplayv2.json",
        ],
    )

    assert dirty_game_ids_from_manifest(
        manifest, endpoints=("shotchartdetail",)
    ) == {"002"}
    assert dirty_game_ids_from_manifest(
        manifest, endpoints=("playbyplayv2",)
    ) == {"003"}
    assert dirty_game_ids_from_manifest(manifest) == {"001", "002", "003"}


def test_dirty_game_ids_from_manifest_ignores_manifest_json() -> None:
    """The run's own manifest.json (season-level) never counts as a dirty game."""
    manifest = _manifest_with_files(files_written=["2024/15/manifest.json"])
    assert dirty_game_ids_from_manifest(manifest) == set()
