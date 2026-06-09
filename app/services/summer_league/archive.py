"""Archive Summer League raw snapshots to durable S3 storage."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from botocore.exceptions import ClientError


class S3ArchiveClient(Protocol):
    """Subset of boto3 S3 client methods used by the archive service."""

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        """Return object metadata."""
        ...

    def put_object(self, **kwargs: Any) -> dict[str, Any] | None:
        """Upload an object."""
        ...


@dataclass(frozen=True)
class S3ArchivePrefix:
    """Parsed destination for raw snapshot archive keys."""

    bucket: str
    prefix: str

    @property
    def uri(self) -> str:
        """Return the canonical S3 URI for this prefix."""
        return (
            f"s3://{self.bucket}/{self.prefix}"
            if self.prefix
            else f"s3://{self.bucket}"
        )


@dataclass(frozen=True)
class ArchiveFilePlan:
    """Archive plan for one local raw file."""

    path: Path
    relative_path: str
    bucket: str
    key: str
    sha256: str
    byte_size: int

    @property
    def s3_uri(self) -> str:
        """Return the destination S3 URI."""
        return f"s3://{self.bucket}/{self.key}"


@dataclass(frozen=True)
class ArchiveFileResult:
    """Archive result for one local raw file."""

    relative_path: str
    s3_key: str
    s3_uri: str
    sha256: str
    byte_size: int
    status: str
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""
        payload: dict[str, Any] = {
            "relative_path": self.relative_path,
            "s3_key": self.s3_key,
            "s3_uri": self.s3_uri,
            "sha256": self.sha256,
            "byte_size": self.byte_size,
            "status": self.status,
        }
        if self.error is not None:
            payload["error"] = self.error
        return payload


@dataclass(frozen=True)
class ArchiveReport:
    """Summary of a raw archive planning or upload run."""

    raw_root: Path
    s3_prefix: S3ArchivePrefix
    dry_run: bool
    force: bool
    files: tuple[ArchiveFileResult, ...]

    @property
    def planned_count(self) -> int:
        """Return files planned but not uploaded because dry-run was enabled."""
        return self._count("planned")

    @property
    def uploaded_count(self) -> int:
        """Return files uploaded to S3."""
        return self._count("uploaded")

    @property
    def skipped_count(self) -> int:
        """Return files skipped because the destination already matched."""
        return self._count("skipped")

    @property
    def error_count(self) -> int:
        """Return files that failed to archive."""
        return self._count("error")

    @property
    def total_count(self) -> int:
        """Return all files considered."""
        return len(self.files)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable report."""
        return {
            "raw_root": self.raw_root.as_posix(),
            "s3_prefix": self.s3_prefix.uri,
            "dry_run": self.dry_run,
            "force": self.force,
            "total_count": self.total_count,
            "planned_count": self.planned_count,
            "uploaded_count": self.uploaded_count,
            "skipped_count": self.skipped_count,
            "error_count": self.error_count,
            "files": [file.to_dict() for file in self.files],
        }

    def to_json(self) -> str:
        """Return a stable pretty JSON report."""
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"

    def _count(self, status: str) -> int:
        return sum(1 for file in self.files if file.status == status)


def parse_s3_archive_prefix(value: str) -> S3ArchivePrefix:
    """Parse an S3 URI into bucket and key prefix."""
    parsed = urlparse(value)
    if parsed.scheme != "s3" or not parsed.netloc:
        raise ValueError("S3 prefix must use s3://<bucket>/<prefix>")
    prefix = parsed.path.strip("/")
    return S3ArchivePrefix(bucket=parsed.netloc, prefix=prefix)


def discover_raw_files(raw_root: Path | str) -> tuple[Path, ...]:
    """Return all regular files under the raw root in deterministic order."""
    root = Path(raw_root)
    if not root.exists():
        raise FileNotFoundError(f"Raw root does not exist: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"Raw root is not a directory: {root}")
    return tuple(sorted(path for path in root.rglob("*") if path.is_file()))


def calculate_sha256(path: Path) -> str:
    """Calculate a file SHA-256 digest without loading large files at once."""
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_archive_plan(
    *,
    raw_root: Path | str,
    s3_prefix: str | S3ArchivePrefix,
) -> tuple[ArchiveFilePlan, ...]:
    """Build a deterministic per-file archive plan."""
    root = Path(raw_root)
    destination = (
        parse_s3_archive_prefix(s3_prefix) if isinstance(s3_prefix, str) else s3_prefix
    )
    plans: list[ArchiveFilePlan] = []
    for path in discover_raw_files(root):
        relative_path = path.relative_to(root).as_posix()
        key = _join_s3_key(destination.prefix, relative_path)
        plans.append(
            ArchiveFilePlan(
                path=path,
                relative_path=relative_path,
                bucket=destination.bucket,
                key=key,
                sha256=calculate_sha256(path),
                byte_size=path.stat().st_size,
            )
        )
    return tuple(plans)


def archive_summer_league_raw(
    *,
    raw_root: Path | str,
    s3_prefix: str | S3ArchivePrefix,
    dry_run: bool,
    force: bool = False,
    s3_client: S3ArchiveClient | None = None,
) -> ArchiveReport:
    """Archive raw snapshot files to S3 and return a structured report."""
    destination = (
        parse_s3_archive_prefix(s3_prefix) if isinstance(s3_prefix, str) else s3_prefix
    )
    plans = build_archive_plan(raw_root=raw_root, s3_prefix=destination)
    results: list[ArchiveFileResult] = []

    if dry_run:
        for plan in plans:
            results.append(_result_from_plan(plan, status="planned"))
        return ArchiveReport(
            raw_root=Path(raw_root),
            s3_prefix=destination,
            dry_run=True,
            force=force,
            files=tuple(results),
        )

    if s3_client is None:
        raise ValueError("s3_client is required unless dry_run=True")

    for plan in plans:
        try:
            if not force and _object_matches_plan(s3_client, plan):
                results.append(_result_from_plan(plan, status="skipped"))
                continue
            _upload_plan(s3_client, plan)
            results.append(_result_from_plan(plan, status="uploaded"))
        except Exception as exc:  # noqa: BLE001 - archive reports per-file failures.
            results.append(
                _result_from_plan(
                    plan,
                    status="error",
                    error=f"{type(exc).__name__}: {exc}",
                )
            )

    return ArchiveReport(
        raw_root=Path(raw_root),
        s3_prefix=destination,
        dry_run=False,
        force=force,
        files=tuple(results),
    )


def write_archive_report(report: ArchiveReport, report_path: Path | str) -> None:
    """Write an archive report JSON file."""
    path = Path(report_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.to_json())


def summarize_report(report: ArchiveReport) -> str:
    """Return a compact one-line report summary for CLI output."""
    return (
        f"raw_files={report.total_count} planned={report.planned_count} "
        f"uploaded={report.uploaded_count} skipped={report.skipped_count} "
        f"errors={report.error_count}"
    )


def _join_s3_key(prefix: str, relative_path: str) -> str:
    return "/".join(part.strip("/") for part in (prefix, relative_path) if part)


def _result_from_plan(
    plan: ArchiveFilePlan,
    *,
    status: str,
    error: str | None = None,
) -> ArchiveFileResult:
    return ArchiveFileResult(
        relative_path=plan.relative_path,
        s3_key=plan.key,
        s3_uri=plan.s3_uri,
        sha256=plan.sha256,
        byte_size=plan.byte_size,
        status=status,
        error=error,
    )


def _object_matches_plan(client: S3ArchiveClient, plan: ArchiveFilePlan) -> bool:
    try:
        response = client.head_object(Bucket=plan.bucket, Key=plan.key)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if str(code) in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise
    metadata = _normalize_metadata(response.get("Metadata", {}))
    remote_sha = metadata.get("sha256")
    remote_size = metadata.get("byte_size")
    if remote_sha == plan.sha256 and remote_size == str(plan.byte_size):
        return True
    content_length = response.get("ContentLength")
    return remote_sha == plan.sha256 and content_length == plan.byte_size


def _normalize_metadata(metadata: object) -> dict[str, str]:
    if not isinstance(metadata, dict):
        return {}
    return {str(key).lower(): str(value) for key, value in metadata.items()}


def _upload_plan(client: S3ArchiveClient, plan: ArchiveFilePlan) -> None:
    client.put_object(
        Bucket=plan.bucket,
        Key=plan.key,
        Body=plan.path.read_bytes(),
        ContentType="application/json",
        Metadata={
            "sha256": plan.sha256,
            "byte_size": str(plan.byte_size),
        },
    )


def result_statuses(results: Iterable[ArchiveFileResult]) -> tuple[str, ...]:
    """Return result statuses, useful for tests and report consumers."""
    return tuple(result.status for result in results)
