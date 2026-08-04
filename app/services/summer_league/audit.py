"""Audit local Summer League raw snapshots into database metadata rows."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.summer_league import (
    SummerLeagueSourceDocument,
    SummerLeagueRawFileStatus,
    SummerLeagueIngestionRun,
    SummerLeagueRawRunStatus,
)
from app.services.summer_league.archive import (
    calculate_sha256,
    parse_s3_archive_prefix,
)
from app.services.summer_league.nba_stats_client import extract_result_sets
from app.services.summer_league.raw_ingestion import GAME_ENDPOINTS

SEASON_ENDPOINTS = ("leaguegamelog_team", "leaguegamelog_player")


@dataclass(frozen=True)
class RawFileDescriptor:
    """Expected raw file location and endpoint identity."""

    relative_path: str
    endpoint: str
    game_id: str | None = None


@dataclass(frozen=True)
class AuditedRawFile:
    """Audit metadata for one expected raw file."""

    descriptor: RawFileDescriptor
    sha256: str | None
    byte_size: int | None
    row_count: int | None
    parse_status: SummerLeagueRawFileStatus
    parse_error: str | None = None
    s3_key: str | None = None


@dataclass(frozen=True)
class AuditedRawRun:
    """Audit metadata for one raw manifest/run."""

    year: int
    league_id: str
    venue_slug: str
    status: SummerLeagueRawRunStatus
    manifest_path: str
    manifest_sha256: str | None
    s3_manifest_key: str | None
    started_at: datetime | None
    finished_at: datetime | None
    team_gamelog_rows: int
    player_gamelog_rows: int
    game_count: int
    error_count: int
    files: tuple[AuditedRawFile, ...]


@dataclass(frozen=True)
class SummerLeagueAuditReport:
    """Structured report for a Summer League raw audit scan."""

    raw_root: Path
    runs: tuple[AuditedRawRun, ...]

    @property
    def runs_scanned(self) -> int:
        """Return number of manifest runs scanned."""
        return len(self.runs)

    @property
    def files_audited(self) -> int:
        """Return number of file rows audited."""
        return sum(len(run.files) for run in self.runs)

    @property
    def parse_failures(self) -> int:
        """Return number of parse-failed files."""
        return sum(
            1
            for run in self.runs
            for file in run.files
            if file.parse_status == SummerLeagueRawFileStatus.PARSE_FAILED
        )

    @property
    def endpoint_coverage(self) -> dict[str, int]:
        """Return audited file counts by endpoint."""
        counts: Counter[str] = Counter()
        for run in self.runs:
            for file in run.files:
                counts[file.descriptor.endpoint] += 1
        return dict(sorted(counts.items()))

    @property
    def row_counts(self) -> dict[str, int]:
        """Return parsed row-count totals by endpoint."""
        counts: Counter[str] = Counter()
        for run in self.runs:
            for file in run.files:
                if file.row_count is not None:
                    counts[file.descriptor.endpoint] += file.row_count
        return dict(sorted(counts.items()))

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable report payload."""
        return {
            "raw_root": self.raw_root.as_posix(),
            "runs_scanned": self.runs_scanned,
            "files_audited": self.files_audited,
            "parse_failures": self.parse_failures,
            "endpoint_coverage": self.endpoint_coverage,
            "row_counts": self.row_counts,
            "runs": [_run_to_dict(run) for run in self.runs],
        }

    def to_json(self) -> str:
        """Return stable pretty JSON for report files."""
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"


async def audit_summer_league_raw(
    db: AsyncSession,
    *,
    raw_root: Path,
    year: int | None = None,
    league_id: str | None = None,
    s3_prefix: str | None = None,
    limit_games: int | None = None,
) -> SummerLeagueAuditReport:
    """Scan local Summer League raw snapshots and upsert audit metadata."""
    runs = tuple(
        audit_raw_run(
            raw_root=raw_root,
            manifest_path=manifest_path,
            s3_prefix=s3_prefix,
            limit_games=limit_games,
        )
        for manifest_path in discover_manifest_paths(
            raw_root=raw_root,
            year=year,
            league_id=league_id,
        )
    )
    for run in runs:
        raw_run = await _upsert_raw_run(db, run)
        await db.flush()
        if raw_run.id is None:
            raise RuntimeError("Raw run id was not populated after flush")
        for audited_file in run.files:
            await _upsert_raw_file(db, raw_run.id, run, audited_file)
    await db.flush()
    return SummerLeagueAuditReport(raw_root=raw_root, runs=runs)


def discover_manifest_paths(
    *,
    raw_root: Path,
    year: int | None = None,
    league_id: str | None = None,
) -> tuple[Path, ...]:
    """Discover manifest files under the raw root, optionally filtered."""
    if not raw_root.exists():
        raise FileNotFoundError(f"Raw root does not exist: {raw_root}")
    if not raw_root.is_dir():
        raise NotADirectoryError(f"Raw root is not a directory: {raw_root}")
    pattern = "*/manifest.json" if year is not None else "*/*/manifest.json"
    search_root = raw_root / str(year) if year is not None else raw_root
    manifests = sorted(search_root.glob(pattern))
    if league_id is not None:
        manifests = [path for path in manifests if path.parent.name == str(league_id)]
    return tuple(path for path in manifests if path.is_file())


def audit_raw_run(
    *,
    raw_root: Path,
    manifest_path: Path,
    s3_prefix: str | None = None,
    limit_games: int | None = None,
) -> AuditedRawRun:
    """Audit one raw manifest and its expected files."""
    manifest_relative_path = manifest_path.relative_to(raw_root).as_posix()
    inferred_year, inferred_league_id = _infer_year_league(manifest_path, raw_root)
    manifest_payload, manifest_error = _read_json_payload(manifest_path)
    manifest_file = inspect_raw_file(
        raw_root=raw_root,
        descriptor=RawFileDescriptor(
            relative_path=manifest_relative_path,
            endpoint="manifest",
        ),
        s3_prefix=s3_prefix,
    )

    if manifest_payload is None:
        return AuditedRawRun(
            year=inferred_year,
            league_id=inferred_league_id,
            venue_slug="unknown",
            status=SummerLeagueRawRunStatus.FAILED,
            manifest_path=manifest_relative_path,
            manifest_sha256=manifest_file.sha256,
            s3_manifest_key=manifest_file.s3_key,
            started_at=None,
            finished_at=None,
            team_gamelog_rows=0,
            player_gamelog_rows=0,
            game_count=0,
            error_count=1 if manifest_error else 0,
            files=(manifest_file,),
        )

    descriptors = build_expected_file_descriptors(
        manifest_relative_path=manifest_relative_path,
        manifest_payload=manifest_payload,
        limit_games=limit_games,
    )
    files = tuple(
        inspect_raw_file(
            raw_root=raw_root,
            descriptor=descriptor,
            s3_prefix=s3_prefix,
        )
        for descriptor in descriptors
    )
    status = _run_status(manifest_payload, files)
    return AuditedRawRun(
        year=int(manifest_payload.get("year") or inferred_year),
        league_id=str(manifest_payload.get("league_id") or inferred_league_id),
        venue_slug=str(manifest_payload.get("venue") or "unknown"),
        status=status,
        manifest_path=manifest_relative_path,
        manifest_sha256=manifest_file.sha256,
        s3_manifest_key=manifest_file.s3_key,
        started_at=_parse_datetime(manifest_payload.get("started_at")),
        finished_at=_parse_datetime(manifest_payload.get("finished_at")),
        team_gamelog_rows=_int_value(manifest_payload.get("team_gamelog_rows")),
        player_gamelog_rows=_int_value(manifest_payload.get("player_gamelog_rows")),
        game_count=_limited_count(
            _int_value(manifest_payload.get("game_count")), limit_games
        ),
        error_count=len(manifest_payload.get("errors") or []),
        files=files,
    )


def build_expected_file_descriptors(
    *,
    manifest_relative_path: str,
    manifest_payload: dict[str, Any],
    limit_games: int | None = None,
) -> tuple[RawFileDescriptor, ...]:
    """Build expected file descriptors from one parsed manifest."""
    year = str(manifest_payload.get("year") or "").strip()
    league_id = str(manifest_payload.get("league_id") or "").strip()
    if not year or not league_id:
        year, league_id = manifest_relative_path.split("/")[:2]
    descriptors = [
        RawFileDescriptor(relative_path=manifest_relative_path, endpoint="manifest")
    ]
    descriptors.extend(
        RawFileDescriptor(
            relative_path=f"{year}/{league_id}/{endpoint}.json",
            endpoint=endpoint,
        )
        for endpoint in SEASON_ENDPOINTS
    )
    game_ids = [str(value) for value in manifest_payload.get("game_ids") or []]
    if limit_games is not None:
        game_ids = game_ids[:limit_games]
    for game_id in game_ids:
        descriptors.extend(
            RawFileDescriptor(
                relative_path=f"{year}/{league_id}/games/{game_id}/{endpoint}.json",
                endpoint=endpoint,
                game_id=game_id,
            )
            for endpoint in GAME_ENDPOINTS
        )
    return tuple(descriptors)


def inspect_raw_file(
    *,
    raw_root: Path,
    descriptor: RawFileDescriptor,
    s3_prefix: str | None = None,
) -> AuditedRawFile:
    """Inspect one expected raw file and return audit metadata."""
    path = raw_root / descriptor.relative_path
    s3_key = _s3_key_for_relative_path(s3_prefix, descriptor.relative_path)
    if not path.exists():
        return AuditedRawFile(
            descriptor=descriptor,
            sha256=None,
            byte_size=None,
            row_count=None,
            parse_status=SummerLeagueRawFileStatus.MISSING,
            parse_error="file missing",
            s3_key=s3_key,
        )
    byte_size = path.stat().st_size
    sha256 = calculate_sha256(path)
    if byte_size == 0:
        return AuditedRawFile(
            descriptor=descriptor,
            sha256=sha256,
            byte_size=byte_size,
            row_count=None,
            parse_status=SummerLeagueRawFileStatus.EMPTY,
            parse_error="file empty",
            s3_key=s3_key,
        )
    payload, error = _read_json_payload(path)
    if payload is None:
        return AuditedRawFile(
            descriptor=descriptor,
            sha256=sha256,
            byte_size=byte_size,
            row_count=None,
            parse_status=SummerLeagueRawFileStatus.PARSE_FAILED,
            parse_error=error,
            s3_key=s3_key,
        )
    return AuditedRawFile(
        descriptor=descriptor,
        sha256=sha256,
        byte_size=byte_size,
        row_count=_primary_row_count(payload),
        parse_status=SummerLeagueRawFileStatus.PARSED,
        s3_key=s3_key,
    )


def write_audit_report(report: SummerLeagueAuditReport, report_path: Path) -> None:
    """Write a raw audit report JSON file."""
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report.to_json())


def summarize_audit_report(report: SummerLeagueAuditReport) -> str:
    """Return a compact one-line summary for CLI output."""
    return (
        f"runs={report.runs_scanned} files={report.files_audited} "
        f"parse_failures={report.parse_failures}"
    )


async def _upsert_raw_run(
    db: AsyncSession,
    run: AuditedRawRun,
) -> SummerLeagueIngestionRun:
    result = await db.execute(
        select(SummerLeagueIngestionRun).where(
            SummerLeagueIngestionRun.year == run.year,  # type: ignore[arg-type]
            SummerLeagueIngestionRun.league_id == run.league_id,  # type: ignore[arg-type]
            SummerLeagueIngestionRun.manifest_path == run.manifest_path,  # type: ignore[arg-type]
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        row = SummerLeagueIngestionRun(
            year=run.year,
            league_id=run.league_id,
            venue_slug=run.venue_slug,
            manifest_path=run.manifest_path,
        )
        db.add(row)
    row.venue_slug = run.venue_slug
    row.status = run.status
    row.started_at = run.started_at
    row.finished_at = run.finished_at
    row.team_gamelog_rows = run.team_gamelog_rows
    row.player_gamelog_rows = run.player_gamelog_rows
    row.game_count = run.game_count
    row.error_count = run.error_count
    row.manifest_sha256 = run.manifest_sha256
    row.s3_manifest_key = run.s3_manifest_key
    row.updated_at = _utc_now_naive()
    return row


async def _upsert_raw_file(
    db: AsyncSession,
    raw_run_id: int,
    run: AuditedRawRun,
    audited_file: AuditedRawFile,
) -> SummerLeagueSourceDocument:
    descriptor = audited_file.descriptor
    result = await db.execute(
        select(SummerLeagueSourceDocument).where(
            SummerLeagueSourceDocument.relative_path == descriptor.relative_path  # type: ignore[arg-type]
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        row = SummerLeagueSourceDocument(
            raw_run_id=raw_run_id,
            year=run.year,
            league_id=run.league_id,
            endpoint=descriptor.endpoint,
            relative_path=descriptor.relative_path,
        )
        db.add(row)
    row.raw_run_id = raw_run_id
    row.year = run.year
    row.league_id = run.league_id
    row.endpoint = descriptor.endpoint
    row.game_id = descriptor.game_id
    row.s3_key = audited_file.s3_key
    row.sha256 = audited_file.sha256
    row.byte_size = audited_file.byte_size
    row.row_count = audited_file.row_count
    row.parse_status = audited_file.parse_status
    row.parse_error = audited_file.parse_error
    row.audited_at = _utc_now_naive()
    row.updated_at = _utc_now_naive()
    return row


def _read_json_payload(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        payload = json.loads(path.read_text())
    except Exception as exc:  # noqa: BLE001 - parse errors are report data.
        return None, f"{type(exc).__name__}: {exc}"
    if not isinstance(payload, dict):
        return None, "expected JSON object"
    return payload, None


def _primary_row_count(payload: dict[str, Any]) -> int | None:
    result_sets = extract_result_sets(payload)
    if not result_sets:
        return None
    return len(result_sets[0].rows)


def _run_status(
    manifest_payload: dict[str, Any],
    files: tuple[AuditedRawFile, ...],
) -> SummerLeagueRawRunStatus:
    if any(
        file.parse_status == SummerLeagueRawFileStatus.PARSE_FAILED for file in files
    ):
        return SummerLeagueRawRunStatus.FAILED
    partial_statuses = {
        SummerLeagueRawFileStatus.MISSING,
        SummerLeagueRawFileStatus.EMPTY,
        SummerLeagueRawFileStatus.SKIPPED,
    }
    if any(file.parse_status in partial_statuses for file in files):
        return SummerLeagueRawRunStatus.PARTIAL
    if manifest_payload.get("errors"):
        return SummerLeagueRawRunStatus.PARTIAL
    return SummerLeagueRawRunStatus.COMPLETE


def _infer_year_league(manifest_path: Path, raw_root: Path) -> tuple[int, str]:
    relative_parts = manifest_path.relative_to(raw_root).parts
    if len(relative_parts) < 3:
        raise ValueError(f"Manifest path is not under year/league: {manifest_path}")
    return int(relative_parts[0]), str(relative_parts[1])


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _int_value(value: object) -> int:
    try:
        return int(str(value or 0))
    except (TypeError, ValueError):
        return 0


def _limited_count(value: int, limit: int | None) -> int:
    if limit is None:
        return value
    return min(value, limit)


def _s3_key_for_relative_path(s3_prefix: str | None, relative_path: str) -> str | None:
    if s3_prefix is None:
        return None
    destination = parse_s3_archive_prefix(s3_prefix)
    return "/".join(
        part.strip("/") for part in (destination.prefix, relative_path) if part
    )


def _utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _run_to_dict(run: AuditedRawRun) -> dict[str, Any]:
    return {
        "year": run.year,
        "league_id": run.league_id,
        "venue_slug": run.venue_slug,
        "status": run.status.value,
        "manifest_path": run.manifest_path,
        "game_count": run.game_count,
        "team_gamelog_rows": run.team_gamelog_rows,
        "player_gamelog_rows": run.player_gamelog_rows,
        "error_count": run.error_count,
        "files": [
            {
                "relative_path": file.descriptor.relative_path,
                "endpoint": file.descriptor.endpoint,
                "game_id": file.descriptor.game_id,
                "status": file.parse_status.value,
                "row_count": file.row_count,
                "parse_error": file.parse_error,
                "s3_key": file.s3_key,
            }
            for file in run.files
        ],
    }
