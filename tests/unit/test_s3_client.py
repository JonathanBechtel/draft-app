"""Unit tests for S3 client URL and upload behavior.

These tests avoid real AWS calls by injecting a fake boto3 client.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from app.config import settings
from app.services.s3_client import S3Client


@dataclass
class _FakeS3:
    last_kwargs: dict[str, Any] | None = None

    def put_object(self, **kwargs: Any) -> None:  # noqa: D401
        self.last_kwargs = kwargs


def test_get_public_url_uses_public_url_base(monkeypatch: pytest.MonkeyPatch) -> None:
    """Uses `S3_PUBLIC_URL_BASE` as a CDN origin for object URLs."""
    monkeypatch.setattr(settings, "s3_bucket_name", "my-bucket")
    monkeypatch.setattr(settings, "s3_public_url_base", "https://cdn.example.com/img")
    monkeypatch.setattr(settings, "image_storage_local", False)

    s3 = S3Client()
    assert s3.get_public_url("players/1_test.png") == (
        "https://cdn.example.com/img/players/1_test.png"
    )


def test_upload_sets_acl_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Includes `ACL` in put_object when `S3_UPLOAD_ACL` is set."""
    monkeypatch.setattr(settings, "s3_bucket_name", "my-bucket")
    monkeypatch.setattr(settings, "s3_upload_acl", "public-read")
    monkeypatch.setattr(settings, "image_storage_local", False)

    s3 = S3Client()
    fake = _FakeS3()
    s3._client = fake  # type: ignore[assignment]

    s3.upload("players/2_test.png", b"abc", content_type="image/png")
    assert fake.last_kwargs is not None
    assert fake.last_kwargs["ACL"] == "public-read"


def test_upload_omits_acl_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Does not include `ACL` in put_object when `S3_UPLOAD_ACL` is unset."""
    monkeypatch.setattr(settings, "s3_bucket_name", "my-bucket")
    monkeypatch.setattr(settings, "s3_upload_acl", None)
    monkeypatch.setattr(settings, "image_storage_local", False)

    s3 = S3Client()
    fake = _FakeS3()
    s3._client = fake  # type: ignore[assignment]

    s3.upload("players/3_test.png", b"abc", content_type="image/png")
    assert fake.last_kwargs is not None
    assert "ACL" not in fake.last_kwargs
