"""Unit tests for Summer League raw audit scanning."""

from __future__ import annotations

import json
from pathlib import Path

from app.schemas.summer_league import (
    SummerLeagueRawFileStatus,
    SummerLeagueRawRunStatus,
)
from app.services.sources.summer_league.audit import (
    audit_raw_run,
    build_expected_file_descriptors,
    discover_manifest_paths,
    inspect_raw_file,
    summarize_audit_report,
    SummerLeagueAuditReport,
)


def _payload(row_count: int, *, game_ids: list[str] | None = None) -> dict[str, object]:
    headers = ["GAME_ID"] if game_ids is not None else ["PLAYER_ID"]
    rows: list[list[object]] = (
        [[game_id] for game_id in game_ids]
        if game_ids is not None
        else [[index] for index in range(row_count)]
    )
    return {"resultSets": [{"name": "Result", "headers": headers, "rowSet": rows}]}


def _write_fixture_run(raw_root: Path) -> None:
    run_dir = raw_root / "2024" / "15"
    game_dir = run_dir / "games" / "1522400001"
    game_dir.mkdir(parents=True)
    manifest = {
        "year": 2024,
        "league_id": "15",
        "venue": "las_vegas",
        "started_at": "2026-06-07T12:00:00Z",
        "finished_at": "2026-06-07T12:04:00Z",
        "team_gamelog_rows": 1,
        "player_gamelog_rows": 2,
        "game_ids": ["1522400001"],
        "game_count": 1,
        "files_written": [],
        "files_skipped": [],
        "errors": [],
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest))
    (run_dir / "leaguegamelog_team.json").write_text(
        json.dumps(_payload(1, game_ids=["1522400001"]))
    )
    (run_dir / "leaguegamelog_player.json").write_text(json.dumps(_payload(2)))
    (game_dir / "boxscoretraditionalv2.json").write_text(json.dumps(_payload(3)))
    (game_dir / "boxscoreadvancedv2.json").write_text(json.dumps(_payload(1)))
    (game_dir / "boxscorescoringv2.json").write_text(json.dumps(_payload(1)))
    (game_dir / "playbyplayv2.json").write_text(json.dumps(_payload(4)))
    (game_dir / "shotchartdetail.json").write_text(json.dumps(_payload(5)))


def test_discover_manifest_paths_supports_filters(tmp_path: Path) -> None:
    """Manifest discovery can scan all runs or a selected year/LeagueID."""
    _write_fixture_run(tmp_path)
    other = tmp_path / "2023" / "13"
    other.mkdir(parents=True)
    (other / "manifest.json").write_text("{}")

    assert [
        path.relative_to(tmp_path).as_posix()
        for path in discover_manifest_paths(raw_root=tmp_path)
    ] == [
        "2023/13/manifest.json",
        "2024/15/manifest.json",
    ]
    assert [
        path.relative_to(tmp_path).as_posix()
        for path in discover_manifest_paths(
            raw_root=tmp_path, year=2024, league_id="15"
        )
    ] == ["2024/15/manifest.json"]


def test_build_expected_file_descriptors_includes_game_endpoints() -> None:
    """Expected file descriptors include manifest, season logs, and game endpoints."""
    descriptors = build_expected_file_descriptors(
        manifest_relative_path="2024/15/manifest.json",
        manifest_payload={"year": 2024, "league_id": "15", "game_ids": ["G1"]},
    )

    assert [descriptor.relative_path for descriptor in descriptors] == [
        "2024/15/manifest.json",
        "2024/15/leaguegamelog_team.json",
        "2024/15/leaguegamelog_player.json",
        "2024/15/games/G1/boxscoretraditionalv2.json",
        "2024/15/games/G1/boxscoreadvancedv2.json",
        "2024/15/games/G1/boxscorescoringv2.json",
        "2024/15/games/G1/playbyplayv2.json",
        "2024/15/games/G1/shotchartdetail.json",
    ]


def test_inspect_raw_file_reports_parsed_row_count_and_s3_key(tmp_path: Path) -> None:
    """Parsed JSON files produce checksums, byte sizes, row counts, and S3 keys."""
    path = tmp_path / "2024" / "15" / "leaguegamelog_player.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_payload(2)))

    audited = inspect_raw_file(
        raw_root=tmp_path,
        descriptor=build_expected_file_descriptors(
            manifest_relative_path="2024/15/manifest.json",
            manifest_payload={"year": 2024, "league_id": "15", "game_ids": []},
        )[2],
        s3_prefix="s3://bucket/raw/nba_stats/summer_league",
    )

    assert audited.parse_status == SummerLeagueRawFileStatus.PARSED
    assert audited.row_count == 2
    assert audited.sha256 is not None
    assert audited.byte_size is not None and audited.byte_size > 0
    assert (
        audited.s3_key
        == "raw/nba_stats/summer_league/2024/15/leaguegamelog_player.json"
    )


def test_inspect_raw_file_reports_missing_empty_and_corrupt(tmp_path: Path) -> None:
    """Missing, empty, and corrupt expected files get explicit statuses."""
    empty = tmp_path / "2024" / "15" / "empty.json"
    corrupt = tmp_path / "2024" / "15" / "corrupt.json"
    empty.parent.mkdir(parents=True)
    empty.write_text("")
    corrupt.write_text("{not-json")

    missing_result = inspect_raw_file(
        raw_root=tmp_path,
        descriptor=build_expected_file_descriptors(
            manifest_relative_path="2024/15/manifest.json",
            manifest_payload={"year": 2024, "league_id": "15", "game_ids": []},
        )[1],
    )
    empty_result = inspect_raw_file(
        raw_root=tmp_path,
        descriptor=missing_result.descriptor.__class__(
            relative_path="2024/15/empty.json",
            endpoint="empty",
        ),
    )
    corrupt_result = inspect_raw_file(
        raw_root=tmp_path,
        descriptor=missing_result.descriptor.__class__(
            relative_path="2024/15/corrupt.json",
            endpoint="corrupt",
        ),
    )

    assert missing_result.parse_status == SummerLeagueRawFileStatus.MISSING
    assert empty_result.parse_status == SummerLeagueRawFileStatus.EMPTY
    assert corrupt_result.parse_status == SummerLeagueRawFileStatus.PARSE_FAILED


def test_audit_raw_run_reports_complete_and_partial_statuses(tmp_path: Path) -> None:
    """Run status summarizes expected file coverage."""
    _write_fixture_run(tmp_path)
    complete = audit_raw_run(
        raw_root=tmp_path, manifest_path=tmp_path / "2024" / "15" / "manifest.json"
    )

    assert complete.status == SummerLeagueRawRunStatus.COMPLETE
    assert len(complete.files) == 8
    assert complete.game_count == 1

    (
        tmp_path / "2024" / "15" / "games" / "1522400001" / "shotchartdetail.json"
    ).unlink()
    partial = audit_raw_run(
        raw_root=tmp_path, manifest_path=tmp_path / "2024" / "15" / "manifest.json"
    )

    assert partial.status == SummerLeagueRawRunStatus.PARTIAL
    assert any(
        file.parse_status == SummerLeagueRawFileStatus.MISSING for file in partial.files
    )


def test_audit_report_summarizes_counts(tmp_path: Path) -> None:
    """Audit reports expose compact totals for CLI output."""
    _write_fixture_run(tmp_path)
    run = audit_raw_run(
        raw_root=tmp_path, manifest_path=tmp_path / "2024" / "15" / "manifest.json"
    )
    report = SummerLeagueAuditReport(raw_root=tmp_path, runs=(run,))

    assert report.files_audited == 8
    assert report.endpoint_coverage["manifest"] == 1
    assert report.row_counts["shotchartdetail"] == 5
    assert summarize_audit_report(report) == "runs=1 files=8 parse_failures=0"
