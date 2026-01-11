"""Unit tests for share card caching behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import settings
from app.services.s3_client import S3Client
from app.services.share_cards.cache_keys import generate_cache_key
from app.services.share_cards.storage import ExportStorage


class TestCacheKeyOrdering:
    """Tests for ordered vs symmetric cache key behavior."""

    def test_vs_arena_cache_key_is_order_sensitive(self) -> None:
        """Uses ordered player_ids for directional vs_arena layout."""
        key_ab = generate_cache_key("vs_arena", [1, 2], {})
        key_ba = generate_cache_key("vs_arena", [2, 1], {})

        assert key_ab != key_ba

    def test_h2h_cache_key_is_order_sensitive(self) -> None:
        """Uses ordered player_ids for directional h2h layout."""
        key_ab = generate_cache_key("h2h", [10, 20], {})
        key_ba = generate_cache_key("h2h", [20, 10], {})

        assert key_ab != key_ba

    def test_other_components_default_to_symmetric(self) -> None:
        """Sorts player_ids for non-directional components for determinism."""
        key_ab = generate_cache_key("performance", [1, 2], {})
        key_ba = generate_cache_key("performance", [2, 1], {})

        assert key_ab == key_ba


class TestLocalCacheMetadata:
    """Tests for local filesystem cache sidecar metadata."""

    def test_local_cache_reads_metadata_sidecar(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Reads title/filename from the local `.json` sidecar without rebuilding models."""
        monkeypatch.setattr(settings, "image_storage_local", True)

        s3 = S3Client()
        s3.local_root = str(tmp_path)

        monkeypatch.setattr("app.services.share_cards.storage.s3_client", s3)
        storage = ExportStorage()

        cache_key = "players/exports/vs_arena/abc123.png"
        storage.upload(
            cache_key,
            b"pngbytes",
            title="Player A — Performance",
            filename="player-a-performance.png",
        )

        cached = storage.check_cache(cache_key)
        assert cached is not None
        assert cached.url == f"/static/img/{cache_key}"
        assert cached.title == "Player A — Performance"
        assert cached.filename == "player-a-performance.png"

