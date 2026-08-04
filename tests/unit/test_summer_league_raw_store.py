"""Unit tests for Summer League raw snapshot storage."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from app.services.sources.summer_league.manifest import SummerLeagueRawManifest
from app.services.sources.summer_league.raw_store import SummerLeagueRawStore


def test_raw_store_builds_deterministic_season_paths(tmp_path: Path) -> None:
    """Season-level snapshots live under year and LeagueID directories."""
    store = SummerLeagueRawStore(tmp_path)

    assert (
        store.season_file(year=2024, league_id="15", name="leaguegamelog_team")
        == tmp_path / "2024" / "15" / "leaguegamelog_team.json"
    )


def test_raw_store_builds_deterministic_game_paths(tmp_path: Path) -> None:
    """Game-level snapshots live under games/{game_id}/{endpoint}.json."""
    store = SummerLeagueRawStore(tmp_path)

    assert (
        store.game_file(
            year="2024",
            league_id="13",
            game_id="1322400001",
            endpoint="playbyplayv2",
        )
        == tmp_path / "2024" / "13" / "games" / "1322400001" / "playbyplayv2.json"
    )


def test_write_json_writes_payload_and_parent_directories(tmp_path: Path) -> None:
    """Writing JSON creates parent directories and stable pretty JSON."""
    store = SummerLeagueRawStore(tmp_path)
    path = store.game_file(
        year=2024,
        league_id="15",
        game_id="1522400076",
        endpoint="boxscoretraditionalv2",
    )

    result = store.write_json(path, {"b": 2, "a": 1})

    assert result.written is True
    assert result.skipped is False
    assert result.relative_path == "2024/15/games/1522400076/boxscoretraditionalv2.json"
    assert json.loads(path.read_text()) == {"a": 1, "b": 2}
    assert path.read_text().endswith("\n")


def test_write_json_skips_existing_file_without_force(tmp_path: Path) -> None:
    """Existing snapshots are reused by default."""
    store = SummerLeagueRawStore(tmp_path)
    path = store.season_file(year=2024, league_id="15", name="leaguegamelog_team")
    first = store.write_json(path, {"version": 1})
    second = store.write_json(path, {"version": 2})

    assert first.written is True
    assert second.written is False
    assert second.skipped is True
    assert json.loads(path.read_text()) == {"version": 1}


def test_write_json_force_overwrites_existing_file(tmp_path: Path) -> None:
    """Force mode refreshes an existing snapshot."""
    store = SummerLeagueRawStore(tmp_path)
    path = store.season_file(year=2024, league_id="15", name="leaguegamelog_player")
    store.write_json(path, {"version": 1})

    result = store.write_json(path, {"version": 2}, force=True)

    assert result.written is True
    assert result.skipped is False
    assert json.loads(path.read_text()) == {"version": 2}


def test_read_json_returns_existing_snapshot_payload(tmp_path: Path) -> None:
    """Existing JSON snapshots can be reused during resumed runs."""
    store = SummerLeagueRawStore(tmp_path)
    path = store.season_file(year=2024, league_id="15", name="leaguegamelog_team")
    store.write_json(path, {"resultSets": []})

    assert store.read_json(path) == {"resultSets": []}


def test_manifest_start_infers_venue_from_league_id() -> None:
    """Manifest setup uses the supported Summer League venue map."""
    manifest = SummerLeagueRawManifest.start(year=2024, league_id="15")

    assert manifest.league_id == "15"
    assert manifest.venue == "las_vegas"
    assert manifest.game_count == 0


def test_manifest_serializes_counts_files_and_errors() -> None:
    """Manifest JSON includes the documented run-summary fields."""
    started_at = datetime(2026, 6, 7, 12, 0, tzinfo=timezone.utc)
    finished_at = datetime(2026, 6, 7, 12, 4, tzinfo=timezone.utc)
    manifest = SummerLeagueRawManifest.start(
        year=2024,
        league_id="15",
        started_at=started_at,
    )
    manifest.finished_at = finished_at
    manifest.team_gamelog_rows = 152
    manifest.player_gamelog_rows = 900
    manifest.game_ids.extend(["1522400001", "1522400002"])
    manifest.record_file("2024/15/leaguegamelog_team.json", written=True)
    manifest.record_file("2024/15/games/1522400001/playbyplayv2.json", written=False)
    manifest.add_error(
        endpoint="playbyplayv2",
        game_id="1522400002",
        message="HTTP 500",
    )

    payload = manifest.to_dict()

    assert payload == {
        "year": 2024,
        "league_id": "15",
        "venue": "las_vegas",
        "started_at": "2026-06-07T12:00:00Z",
        "finished_at": "2026-06-07T12:04:00Z",
        "team_gamelog_rows": 152,
        "player_gamelog_rows": 900,
        "game_ids": ["1522400001", "1522400002"],
        "game_count": 2,
        "files_written": ["2024/15/leaguegamelog_team.json"],
        "files_skipped": ["2024/15/games/1522400001/playbyplayv2.json"],
        "errors": [
            {
                "endpoint": "playbyplayv2",
                "game_id": "1522400002",
                "message": "HTTP 500",
            }
        ],
    }
    assert json.loads(manifest.to_json()) == payload


def test_write_manifest_uses_canonical_manifest_path(tmp_path: Path) -> None:
    """Manifest writes land at manifest.json for the year and LeagueID."""
    store = SummerLeagueRawStore(tmp_path)
    manifest = SummerLeagueRawManifest.start(year=2024, league_id="13")
    manifest.game_ids.append("1322400001")

    result = store.write_manifest(manifest)

    assert result.written is True
    assert result.relative_path == "2024/13/manifest.json"
    payload = json.loads((tmp_path / "2024" / "13" / "manifest.json").read_text())
    assert payload["venue"] == "california_classic"
    assert payload["game_count"] == 1
