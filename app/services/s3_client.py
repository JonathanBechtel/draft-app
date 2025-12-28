"""S3 client wrapper for image storage operations."""

import logging
from typing import Optional

import boto3
from botocore.exceptions import ClientError

from app.config import settings

logger = logging.getLogger(__name__)


class S3Client:
    """Thin wrapper around boto3 for S3 operations.

    Supports both AWS S3 and S3-compatible services (Tigris, R2, MinIO).
    Falls back to local filesystem storage when image_storage_local is True.
    """

    def __init__(self) -> None:
        self.bucket = settings.s3_bucket_name
        self.public_url_base = settings.s3_public_url_base
        self.use_local = settings.image_storage_local
        self._client: Optional[boto3.client] = None

    @property
    def client(self) -> boto3.client:
        """Lazily initialize the S3 client."""
        if self._client is None:
            client_kwargs: dict[str, str] = {
                "region_name": settings.s3_region,
            }
            if settings.s3_access_key_id and settings.s3_secret_access_key:
                client_kwargs["aws_access_key_id"] = settings.s3_access_key_id
                client_kwargs["aws_secret_access_key"] = settings.s3_secret_access_key
            if settings.s3_endpoint_url:
                client_kwargs["endpoint_url"] = settings.s3_endpoint_url

            self._client = boto3.client("s3", **client_kwargs)
        return self._client

    def upload(
        self,
        key: str,
        data: bytes,
        content_type: str = "image/png",
    ) -> str:
        """Upload file to S3, return public URL.

        Args:
            key: S3 object key (e.g., 'players/123_cooper-flagg_default.png')
            data: File content as bytes
            content_type: MIME type of the file

        Returns:
            Public URL for the uploaded file
        """
        if self.use_local:
            return self._upload_local(key, data)

        if not self.bucket:
            raise ValueError("S3_BUCKET_NAME not configured")

        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
                # Make publicly readable
                ACL="public-read",
            )
            logger.info(f"Uploaded {key} to S3 bucket {self.bucket}")
            return self.get_public_url(key)
        except ClientError as e:
            logger.error(f"Failed to upload {key} to S3: {e}")
            raise

    def delete(self, key: str) -> None:
        """Delete file from S3.

        Args:
            key: S3 object key to delete
        """
        if self.use_local:
            self._delete_local(key)
            return

        if not self.bucket:
            raise ValueError("S3_BUCKET_NAME not configured")

        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
            logger.info(f"Deleted {key} from S3 bucket {self.bucket}")
        except ClientError as e:
            logger.error(f"Failed to delete {key} from S3: {e}")
            raise

    def get_public_url(self, key: str) -> str:
        """Return public URL for a key.

        Uses configured public_url_base (CDN) if available,
        otherwise constructs direct S3 URL.

        Args:
            key: S3 object key

        Returns:
            Full public URL to access the object
        """
        if self.use_local:
            return f"/static/img/{key}"

        if self.public_url_base:
            base = self.public_url_base.rstrip("/")
            return f"{base}/{key}"

        # Fallback to direct S3 URL
        if settings.s3_endpoint_url:
            endpoint = settings.s3_endpoint_url.rstrip("/")
            return f"{endpoint}/{self.bucket}/{key}"

        return f"https://{self.bucket}.s3.{settings.s3_region}.amazonaws.com/{key}"

    def _upload_local(self, key: str, data: bytes) -> str:
        """Upload to local filesystem (dev mode).

        Args:
            key: Relative path within static/img/
            data: File content as bytes

        Returns:
            Local static URL path
        """
        import os

        local_path = f"app/static/img/{key}"
        os.makedirs(os.path.dirname(local_path), exist_ok=True)

        with open(local_path, "wb") as f:
            f.write(data)

        logger.info(f"Saved {key} to local filesystem")
        return f"/static/img/{key}"

    def _delete_local(self, key: str) -> None:
        """Delete from local filesystem (dev mode).

        Args:
            key: Relative path within static/img/
        """
        import os

        local_path = f"app/static/img/{key}"
        if os.path.exists(local_path):
            os.remove(local_path)
            logger.info(f"Deleted {key} from local filesystem")


# Singleton instance for convenience
s3_client = S3Client()
