"""Unit tests for Summer League raw archive planning and uploads."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.services.sources.summer_league.archive import (
    archive_summer_league_raw,
    build_archive_plan,
    parse_s3_archive_prefix,
    result_statuses,
    write_archive_report,
)
from scripts import archive_summer_league_raw as archive_cli


class FakeS3Client:
    """Fake S3 client for archive unit tests."""

    def __init__(
        self,
        existing_metadata: dict[tuple[str, str], dict[str, Any]] | None = None,
    ) -> None:
        self.existing_metadata = existing_metadata or {}
        self.head_calls: list[dict[str, Any]] = []
        self.put_calls: list[dict[str, Any]] = []

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        """Return fake object metadata or a lightweight missing-object error."""
        self.head_calls.append(kwargs)
        key = (str(kwargs["Bucket"]), str(kwargs["Key"]))
        if key not in self.existing_metadata:
            from botocore.exceptions import ClientError

            raise ClientError(
                {"Error": {"Code": "404", "Message": "Not Found"}},
                "HeadObject",
            )
        return self.existing_metadata[key]

    def put_object(self, **kwargs: Any) -> None:
        """Record fake uploads."""
        self.put_calls.append(kwargs)


def _write_raw_fixture(root: Path) -> None:
    (root / "2024" / "15" / "games" / "1522400001").mkdir(parents=True)
    (root / "2024" / "15" / "manifest.json").write_text('{"game_count": 1}\n')
    (
        root / "2024" / "15" / "games" / "1522400001" / "boxscoretraditionalv2.json"
    ).write_text('{"resultSets": []}\n')


def test_parse_s3_archive_prefix_accepts_bucket_and_prefix() -> None:
    """S3 prefixes are parsed into a bucket and normalized key prefix."""
    prefix = parse_s3_archive_prefix("s3://draftguru-raw/raw/nba_stats/summer_league/")

    assert prefix.bucket == "draftguru-raw"
    assert prefix.prefix == "raw/nba_stats/summer_league"


def test_parse_s3_archive_prefix_rejects_non_s3_uri() -> None:
    """Archive destinations must be S3 URIs."""
    with pytest.raises(ValueError, match="s3://"):
        parse_s3_archive_prefix("https://example.com/raw")


def test_build_archive_plan_preserves_relative_paths(tmp_path: Path) -> None:
    """Local relative paths are appended exactly to the archive prefix."""
    _write_raw_fixture(tmp_path)

    plan = build_archive_plan(
        raw_root=tmp_path,
        s3_prefix="s3://draftguru-raw/raw/nba_stats/summer_league",
    )

    assert [item.relative_path for item in plan] == [
        "2024/15/games/1522400001/boxscoretraditionalv2.json",
        "2024/15/manifest.json",
    ]
    assert [item.key for item in plan] == [
        "raw/nba_stats/summer_league/2024/15/games/1522400001/boxscoretraditionalv2.json",
        "raw/nba_stats/summer_league/2024/15/manifest.json",
    ]
    assert all(item.sha256 for item in plan)
    assert all(item.byte_size > 0 for item in plan)


def test_dry_run_reports_planned_files_without_s3_calls(tmp_path: Path) -> None:
    """Dry-run reports planned objects and never touches S3."""
    _write_raw_fixture(tmp_path)
    fake = FakeS3Client()

    report = archive_summer_league_raw(
        raw_root=tmp_path,
        s3_prefix="s3://draftguru-raw/raw/nba_stats/summer_league",
        dry_run=True,
        s3_client=fake,
    )

    assert report.total_count == 2
    assert report.planned_count == 2
    assert result_statuses(report.files) == ("planned", "planned")
    assert fake.head_calls == []
    assert fake.put_calls == []


def test_upload_skips_unchanged_objects_by_checksum_and_size(tmp_path: Path) -> None:
    """Upload mode skips objects with matching archive metadata."""
    _write_raw_fixture(tmp_path)
    plan = build_archive_plan(
        raw_root=tmp_path,
        s3_prefix="s3://draftguru-raw/raw/nba_stats/summer_league",
    )
    fake = FakeS3Client(
        {
            (item.bucket, item.key): {
                "Metadata": {
                    "sha256": item.sha256,
                    "byte_size": str(item.byte_size),
                },
                "ContentLength": item.byte_size,
            }
            for item in plan
        }
    )

    report = archive_summer_league_raw(
        raw_root=tmp_path,
        s3_prefix="s3://draftguru-raw/raw/nba_stats/summer_league",
        dry_run=False,
        s3_client=fake,
    )

    assert report.skipped_count == 2
    assert report.uploaded_count == 0
    assert fake.put_calls == []


def test_upload_sends_private_objects_with_checksum_metadata(tmp_path: Path) -> None:
    """Missing destination objects are uploaded with checksum and size metadata."""
    _write_raw_fixture(tmp_path)
    fake = FakeS3Client()

    report = archive_summer_league_raw(
        raw_root=tmp_path,
        s3_prefix="s3://draftguru-raw/raw/nba_stats/summer_league",
        dry_run=False,
        s3_client=fake,
    )

    assert report.uploaded_count == 2
    assert len(fake.put_calls) == 2
    first = fake.put_calls[0]
    assert first["Bucket"] == "draftguru-raw"
    assert first["Key"].startswith("raw/nba_stats/summer_league/2024/15/")
    assert first["ContentType"] == "application/json"
    assert first["Metadata"]["sha256"]
    assert first["Metadata"]["byte_size"].isdigit()


def test_force_uploads_even_when_remote_metadata_matches(tmp_path: Path) -> None:
    """Force mode bypasses unchanged-object skip logic."""
    _write_raw_fixture(tmp_path)
    plan = build_archive_plan(
        raw_root=tmp_path,
        s3_prefix="s3://draftguru-raw/raw/nba_stats/summer_league",
    )
    fake = FakeS3Client(
        {
            (item.bucket, item.key): {
                "Metadata": {
                    "sha256": item.sha256,
                    "byte_size": str(item.byte_size),
                }
            }
            for item in plan
        }
    )

    report = archive_summer_league_raw(
        raw_root=tmp_path,
        s3_prefix="s3://draftguru-raw/raw/nba_stats/summer_league",
        dry_run=False,
        force=True,
        s3_client=fake,
    )

    assert report.uploaded_count == 2
    assert fake.head_calls == []
    assert len(fake.put_calls) == 2


def test_write_archive_report_outputs_json_summary(tmp_path: Path) -> None:
    """Archive reports can be persisted for operator review."""
    _write_raw_fixture(tmp_path / "raw")
    report = archive_summer_league_raw(
        raw_root=tmp_path / "raw",
        s3_prefix="s3://draftguru-raw/raw/nba_stats/summer_league",
        dry_run=True,
    )
    report_path = tmp_path / "reports" / "archive.json"

    write_archive_report(report, report_path)

    payload = json.loads(report_path.read_text())
    assert payload["total_count"] == 2
    assert payload["planned_count"] == 2
    assert payload["files"][0]["s3_uri"].startswith(
        "s3://draftguru-raw/raw/nba_stats/summer_league/"
    )


def test_cli_dry_run_prints_summary_and_writes_report(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The CLI dry-run path does not require real S3 configuration."""
    _write_raw_fixture(tmp_path / "raw")
    report_path = tmp_path / "archive-report.json"

    exit_code = archive_cli.main(
        [
            "--raw-root",
            str(tmp_path / "raw"),
            "--s3-prefix",
            "s3://draftguru-raw/raw/nba_stats/summer_league",
            "--dry-run",
            "--report-path",
            str(report_path),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "raw_files=2 planned=2 uploaded=0 skipped=0 errors=0" in captured.out
    assert json.loads(report_path.read_text())["planned_count"] == 2
