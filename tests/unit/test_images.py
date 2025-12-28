"""Unit tests for image utility functions."""

import os
import tempfile

from app.utils.images import (
    DEFAULT_STYLE,
    IMAGE_STYLES,
    PLAYER_IMAGES_DIR,
    get_available_styles,
    get_placeholder_url,
    get_player_photo_url,
)


class TestGetPlayerPhotoUrl:
    """Tests for get_player_photo_url function."""

    def test_returns_placeholder_when_no_image_exists(self):
        """Should return placehold.co URL when no local image file exists."""
        url = get_player_photo_url(999, "test-player", "Test Player")

        assert "placehold.co" in url
        assert "Test+Player" in url

    def test_placeholder_uses_display_name(self):
        """Should include display name in placeholder URL."""
        url = get_player_photo_url(123, "cooper-flagg", "Cooper Flagg")

        assert "Cooper+Flagg" in url

    def test_placeholder_falls_back_to_player_id(self):
        """Should use 'Player {id}' when no display name provided."""
        url = get_player_photo_url(42, "unknown-player", None)

        assert "Player+42" in url

    def test_returns_new_format_png_when_exists(self):
        """Should return new format path {id}_{slug}_{style}.png when it exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            from app.utils import images

            original_dir = images.PLAYER_IMAGES_DIR
            images.PLAYER_IMAGES_DIR = tmpdir

            try:
                # Create new format image file
                image_path = os.path.join(tmpdir, "1_cooper-flagg_default.png")
                with open(image_path, "w") as f:
                    f.write("fake image")

                url = images.get_player_photo_url(1, "cooper-flagg", "Cooper Flagg")

                assert url == "/static/img/players/1_cooper-flagg_default.png"
            finally:
                images.PLAYER_IMAGES_DIR = original_dir

    def test_returns_legacy_format_jpg_when_no_new_format(self):
        """Should fall back to legacy format {id}_{style}.jpg when new format doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            from app.utils import images

            original_dir = images.PLAYER_IMAGES_DIR
            images.PLAYER_IMAGES_DIR = tmpdir

            try:
                # Create legacy format image file
                image_path = os.path.join(tmpdir, "1_default.jpg")
                with open(image_path, "w") as f:
                    f.write("fake image")

                url = images.get_player_photo_url(1, "cooper-flagg", "Cooper Flagg")

                assert url == "/static/img/players/1_default.jpg"
            finally:
                images.PLAYER_IMAGES_DIR = original_dir

    def test_prefers_new_format_over_legacy(self):
        """Should prefer new format when both exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            from app.utils import images

            original_dir = images.PLAYER_IMAGES_DIR
            images.PLAYER_IMAGES_DIR = tmpdir

            try:
                # Create both formats
                new_path = os.path.join(tmpdir, "1_cooper-flagg_default.png")
                legacy_path = os.path.join(tmpdir, "1_default.jpg")
                for path in [new_path, legacy_path]:
                    with open(path, "w") as f:
                        f.write("fake image")

                url = images.get_player_photo_url(1, "cooper-flagg", "Cooper Flagg")

                # Should return new format
                assert url == "/static/img/players/1_cooper-flagg_default.png"
            finally:
                images.PLAYER_IMAGES_DIR = original_dir

    def test_returns_requested_style_when_exists(self):
        """Should return path with requested style when that image exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            from app.utils import images

            original_dir = images.PLAYER_IMAGES_DIR
            images.PLAYER_IMAGES_DIR = tmpdir

            try:
                # Create vector style image (new format)
                image_path = os.path.join(tmpdir, "1_cooper-flagg_vector.png")
                with open(image_path, "w") as f:
                    f.write("fake image")

                url = images.get_player_photo_url(
                    1, "cooper-flagg", "Cooper Flagg", style="vector"
                )

                assert url == "/static/img/players/1_cooper-flagg_vector.png"
            finally:
                images.PLAYER_IMAGES_DIR = original_dir

    def test_falls_back_to_default_when_requested_style_missing(self):
        """Should fall back to default style when requested style doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            from app.utils import images

            original_dir = images.PLAYER_IMAGES_DIR
            images.PLAYER_IMAGES_DIR = tmpdir

            try:
                # Create only default style image (new format)
                image_path = os.path.join(tmpdir, "1_cooper-flagg_default.png")
                with open(image_path, "w") as f:
                    f.write("fake image")

                url = images.get_player_photo_url(
                    1, "cooper-flagg", "Cooper Flagg", style="comic"
                )

                assert url == "/static/img/players/1_cooper-flagg_default.png"
            finally:
                images.PLAYER_IMAGES_DIR = original_dir

    def test_falls_back_to_legacy_default_when_style_missing(self):
        """Should fall back to legacy default when requested style and new format don't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            from app.utils import images

            original_dir = images.PLAYER_IMAGES_DIR
            images.PLAYER_IMAGES_DIR = tmpdir

            try:
                # Create only legacy default
                image_path = os.path.join(tmpdir, "1_default.jpg")
                with open(image_path, "w") as f:
                    f.write("fake image")

                url = images.get_player_photo_url(
                    1, "cooper-flagg", "Cooper Flagg", style="comic"
                )

                assert url == "/static/img/players/1_default.jpg"
            finally:
                images.PLAYER_IMAGES_DIR = original_dir

    def test_returns_placeholder_when_neither_style_nor_default_exists(self):
        """Should return placeholder when neither requested nor default style exists."""
        url = get_player_photo_url(999, "test-player", "Test Player", style="vector")

        assert "placehold.co" in url


class TestGetAvailableStyles:
    """Tests for get_available_styles function."""

    def test_returns_empty_list_when_no_images(self):
        """Should return empty list when no images exist for player."""
        styles = get_available_styles(999, "test-player")

        assert styles == []

    def test_returns_available_styles_new_format(self):
        """Should return list of styles for new format images."""
        with tempfile.TemporaryDirectory() as tmpdir:
            from app.utils import images

            original_dir = images.PLAYER_IMAGES_DIR
            images.PLAYER_IMAGES_DIR = tmpdir

            try:
                # Create some style images in new format
                for style in ["default", "vector"]:
                    image_path = os.path.join(tmpdir, f"1_cooper-flagg_{style}.png")
                    with open(image_path, "w") as f:
                        f.write("fake image")

                styles = images.get_available_styles(1, "cooper-flagg")

                assert "default" in styles
                assert "vector" in styles
                assert "comic" not in styles
                assert "retro" not in styles
            finally:
                images.PLAYER_IMAGES_DIR = original_dir

    def test_returns_available_styles_legacy_format(self):
        """Should return list of styles for legacy format images."""
        with tempfile.TemporaryDirectory() as tmpdir:
            from app.utils import images

            original_dir = images.PLAYER_IMAGES_DIR
            images.PLAYER_IMAGES_DIR = tmpdir

            try:
                # Create some style images in legacy format
                for style in ["default", "comic"]:
                    image_path = os.path.join(tmpdir, f"1_{style}.jpg")
                    with open(image_path, "w") as f:
                        f.write("fake image")

                styles = images.get_available_styles(1, "cooper-flagg")

                assert "default" in styles
                assert "comic" in styles
                assert "vector" not in styles
            finally:
                images.PLAYER_IMAGES_DIR = original_dir

    def test_returns_mixed_format_styles(self):
        """Should detect styles from both new and legacy formats."""
        with tempfile.TemporaryDirectory() as tmpdir:
            from app.utils import images

            original_dir = images.PLAYER_IMAGES_DIR
            images.PLAYER_IMAGES_DIR = tmpdir

            try:
                # Create new format for default
                new_path = os.path.join(tmpdir, "1_cooper-flagg_default.png")
                with open(new_path, "w") as f:
                    f.write("fake image")

                # Create legacy format for comic
                legacy_path = os.path.join(tmpdir, "1_comic.jpg")
                with open(legacy_path, "w") as f:
                    f.write("fake image")

                styles = images.get_available_styles(1, "cooper-flagg")

                assert "default" in styles
                assert "comic" in styles
            finally:
                images.PLAYER_IMAGES_DIR = original_dir


class TestGetPlaceholderUrl:
    """Tests for get_placeholder_url function."""

    def test_generates_placeholder_with_name(self):
        """Should generate placeholder URL with display name."""
        url = get_placeholder_url("Cooper Flagg")

        assert "placehold.co" in url
        assert "Cooper+Flagg" in url

    def test_generates_placeholder_with_player_id(self):
        """Should use player ID when no name provided."""
        url = get_placeholder_url(player_id=42)

        assert "Player+42" in url


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
