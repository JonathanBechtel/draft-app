"""Storage layer for share card exports using S3 or local filesystem."""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from botocore.exceptions import ClientError

from app.services.s3_client import s3_client

logger = logging.getLogger(__name__)

_META_TITLE_KEY = "dg_title_b64"
_META_FILENAME_KEY = "dg_filename_b64"


@dataclass(frozen=True)
class CachedExport:
    """Cached export result with optional response metadata."""

    url: str
    title: str | None = None
    filename: str | None = None


def _encode_metadata_value(value: str) -> str:
    """Encode a potentially non-ASCII string as urlsafe base64 for S3 metadata."""
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii")


def _decode_metadata_value(value: str) -> str | None:
    """Decode urlsafe base64 S3 metadata; returns None if invalid."""
    try:
        return base64.urlsafe_b64decode(value.encode("ascii")).decode("utf-8")
    except Exception:  # noqa: BLE001
        return None


class ExportStorage:
    """Storage wrapper for share card exports with cache support."""

    def __init__(self) -> None:
        """Initialize the storage layer."""
        self._s3 = s3_client

    def check_cache(self, cache_key: str) -> CachedExport | None:
        """Check if an export already exists in storage.

        Args:
            cache_key: S3 key to check (e.g., "exports/performance/abc123.png")

        Returns:
            Cached export info if exists, None otherwise
        """
        if self._s3.use_local:
            return self._check_local_cache(cache_key)

        if not self._s3.bucket:
            return None

        try:
            result = self._s3.client.head_object(Bucket=self._s3.bucket, Key=cache_key)
            url = self._s3.get_public_url(cache_key)
            metadata = result.get("Metadata", {}) if isinstance(result, dict) else {}
            title = _decode_metadata_value(metadata.get(_META_TITLE_KEY, ""))
            filename = _decode_metadata_value(metadata.get(_META_FILENAME_KEY, ""))
            logger.debug(f"Cache hit for {cache_key}")
            return CachedExport(url=url, title=title, filename=filename)
        except ClientError as e:
            if e.response["Error"]["Code"] == "404":
                logger.debug(f"Cache miss for {cache_key}")
                return None
            logger.warning(f"Error checking cache for {cache_key}: {e}")
            return None

    def upload(
        self, cache_key: str, png_bytes: bytes, *, title: str, filename: str
    ) -> str:
        """Upload PNG to storage.

        Args:
            cache_key: S3 key to store at
            png_bytes: PNG image bytes
            title: Human-readable display title for the export
            filename: Download filename for the export

        Returns:
            Public URL for the uploaded image
        """
        return self._s3.upload(
            key=cache_key,
            data=png_bytes,
            content_type="image/png",
            metadata={
                _META_TITLE_KEY: _encode_metadata_value(title),
                _META_FILENAME_KEY: _encode_metadata_value(filename),
            },
        )

    def _check_local_cache(self, cache_key: str) -> CachedExport | None:
        """Check local filesystem cache.

        Args:
            cache_key: Relative path within static/img/

        Returns:
            Cached export info if exists, None otherwise
        """
        local_path = Path(self._s3.local_root) / cache_key
        if local_path.exists():
            title: str | None = None
            filename: str | None = None
            meta_path = Path(f"{local_path}.json")
            if meta_path.exists():
                try:
                    with meta_path.open("r", encoding="utf-8") as f:
                        meta = json.load(f)
                    if isinstance(meta, dict):
                        raw_title = meta.get(_META_TITLE_KEY)
                        raw_filename = meta.get(_META_FILENAME_KEY)
                        if isinstance(raw_title, str):
                            title = _decode_metadata_value(raw_title) or raw_title
                        if isinstance(raw_filename, str):
                            filename = (
                                _decode_metadata_value(raw_filename) or raw_filename
                            )
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        f"Failed reading local export metadata: {meta_path}: {e}"
                    )

            logger.debug(f"Local cache hit for {cache_key}")
            return CachedExport(
                url=f"/static/img/{cache_key}",
                title=title,
                filename=filename,
            )
        logger.debug(f"Local cache miss for {cache_key}")
        return None


# Module-level singleton
_storage: ExportStorage | None = None


def get_export_storage() -> ExportStorage:
    """Get or create the export storage singleton."""
    global _storage
    if _storage is None:
        _storage = ExportStorage()
    return _storage
