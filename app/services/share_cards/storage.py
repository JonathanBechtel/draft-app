"""Storage layer for share card exports using S3 or local filesystem."""

import logging
from typing import Optional

from botocore.exceptions import ClientError

from app.services.s3_client import s3_client

logger = logging.getLogger(__name__)


class ExportStorage:
    """Storage wrapper for share card exports with cache support."""

    def __init__(self) -> None:
        """Initialize the storage layer."""
        self._s3 = s3_client

    def check_cache(self, cache_key: str) -> Optional[str]:
        """Check if an export already exists in storage.

        Args:
            cache_key: S3 key to check (e.g., "exports/performance/abc123.png")

        Returns:
            Public URL if exists, None otherwise
        """
        if self._s3.use_local:
            return self._check_local_cache(cache_key)

        if not self._s3.bucket:
            return None

        try:
            self._s3.client.head_object(Bucket=self._s3.bucket, Key=cache_key)
            url = self._s3.get_public_url(cache_key)
            logger.debug(f"Cache hit for {cache_key}")
            return url
        except ClientError as e:
            if e.response["Error"]["Code"] == "404":
                logger.debug(f"Cache miss for {cache_key}")
                return None
            logger.warning(f"Error checking cache for {cache_key}: {e}")
            return None

    def upload(self, cache_key: str, png_bytes: bytes) -> str:
        """Upload PNG to storage.

        Args:
            cache_key: S3 key to store at
            png_bytes: PNG image bytes

        Returns:
            Public URL for the uploaded image
        """
        return self._s3.upload(
            key=cache_key,
            data=png_bytes,
            content_type="image/png",
        )

    def _check_local_cache(self, cache_key: str) -> Optional[str]:
        """Check local filesystem cache.

        Args:
            cache_key: Relative path within static/img/

        Returns:
            Local URL if exists, None otherwise
        """
        import os

        local_path = f"app/static/img/{cache_key}"
        if os.path.exists(local_path):
            logger.debug(f"Local cache hit for {cache_key}")
            return f"/static/img/{cache_key}"
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
