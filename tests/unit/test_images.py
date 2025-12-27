"""Unit tests for image utility functions."""

import os
import tempfile
from unittest.mock import patch

from app.utils.images import (
    DEFAULT_STYLE,
    IMAGE_STYLES,
    PLAYER_IMAGES_DIR,
    get_available_styles,
    get_player_photo_url,
)


class TestGetPlayerPhotoUrl:
    """Tests for get_player_photo_url function."""

    def test_returns_placeholder_when_no_image_exists(self):
        """Should return placehold.co URL when no local image file exists."""
        url = get_player_photo_url(999, "Test Player")

        assert "placehold.co" in url
        assert "Test+Player" in url

    def test_placeholder_uses_display_name(self):
        """Should include display name in placeholder URL."""
        url = get_player_photo_url(123, "Cooper Flagg")

        assert "Cooper+Flagg" in url

    def test_placeholder_falls_back_to_player_id(self):
        """Should use 'Player {id}' when no display name provided."""
        url = get_player_photo_url(42, None)

        assert "Player+42" in url

    def test_returns_local_path_when_default_image_exists(self):
        """Should return local static path when default image file exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(
                __import__("app.utils.images", fromlist=["PLAYER_IMAGES_DIR"]),
                "PLAYER_IMAGES_DIR",
                tmpdir,
            ):
                # Reimport to pick up patched value
                from app.utils import images

                original_dir = images.PLAYER_IMAGES_DIR
                images.PLAYER_IMAGES_DIR = tmpdir

                try:
                    # Create test image file
                    image_path = os.path.join(tmpdir, "1_default.jpg")
                    with open(image_path, "w") as f:
                        f.write("fake image")

                    url = images.get_player_photo_url(1, "Cooper Flagg")

                    assert url == "/static/img/players/1_default.jpg"
                finally:
                    images.PLAYER_IMAGES_DIR = original_dir

    def test_returns_requested_style_when_exists(self):
        """Should return path with requested style when that image exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            from app.utils import images

            original_dir = images.PLAYER_IMAGES_DIR
            images.PLAYER_IMAGES_DIR = tmpdir

            try:
                # Create vector style image
                image_path = os.path.join(tmpdir, "1_vector.jpg")
                with open(image_path, "w") as f:
                    f.write("fake image")

                url = images.get_player_photo_url(1, "Cooper Flagg", style="vector")

                assert url == "/static/img/players/1_vector.jpg"
            finally:
                images.PLAYER_IMAGES_DIR = original_dir

    def test_falls_back_to_default_when_requested_style_missing(self):
        """Should fall back to default style when requested style doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            from app.utils import images

            original_dir = images.PLAYER_IMAGES_DIR
            images.PLAYER_IMAGES_DIR = tmpdir

            try:
                # Create only default style image
                image_path = os.path.join(tmpdir, "1_default.jpg")
                with open(image_path, "w") as f:
                    f.write("fake image")

                url = images.get_player_photo_url(1, "Cooper Flagg", style="comic")

                assert url == "/static/img/players/1_default.jpg"
            finally:
                images.PLAYER_IMAGES_DIR = original_dir

    def test_returns_placeholder_when_neither_style_nor_default_exists(self):
        """Should return placeholder when neither requested nor default style exists."""
        url = get_player_photo_url(999, "Test Player", style="vector")

        assert "placehold.co" in url


class TestGetAvailableStyles:
    """Tests for get_available_styles function."""

    def test_returns_empty_list_when_no_images(self):
        """Should return empty list when no images exist for player."""
        styles = get_available_styles(999)

        assert styles == []

    def test_returns_available_styles(self):
        """Should return list of styles that have corresponding image files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            from app.utils import images

            original_dir = images.PLAYER_IMAGES_DIR
            images.PLAYER_IMAGES_DIR = tmpdir

            try:
                # Create some style images
                for style in ["default", "vector"]:
                    image_path = os.path.join(tmpdir, f"1_{style}.jpg")
                    with open(image_path, "w") as f:
                        f.write("fake image")

                styles = images.get_available_styles(1)

                assert "default" in styles
                assert "vector" in styles
                assert "comic" not in styles
                assert "retro" not in styles
            finally:
                images.PLAYER_IMAGES_DIR = original_dir


class TestConstants:
    """Tests for module constants."""

    def test_image_styles_includes_expected_values(self):
        """Should include the expected style options."""
        assert "default" in IMAGE_STYLES
        assert "vector" in IMAGE_STYLES
        assert "comic" in IMAGE_STYLES
        assert "retro" in IMAGE_STYLES

    def test_default_style_is_default(self):
        """Default style should be 'default'."""
        assert DEFAULT_STYLE == "default"

    def test_player_images_dir_path(self):
        """Player images directory should point to static assets."""
        assert "static/img/players" in PLAYER_IMAGES_DIR
