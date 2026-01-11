"""SVG to PNG rasterization using resvg-py."""

import logging
from io import BytesIO
from pathlib import Path

from PIL import Image

from app.services.share_cards.constants import (
    OUTPUT_HEIGHT,
    OUTPUT_WIDTH,
    RENDER_HEIGHT,
    RENDER_WIDTH,
)

logger = logging.getLogger(__name__)

# Fonts directory path
FONTS_DIR = Path(__file__).parent.parent.parent / "static" / "fonts"


class Rasterizer:
    """Converts SVG to PNG using resvg-py."""

    def __init__(self) -> None:
        """Initialize the rasterizer with font paths."""
        self.font_dir = str(FONTS_DIR) if FONTS_DIR.exists() else None
        if not self.font_dir:
            logger.warning(f"Fonts directory not found: {FONTS_DIR}")

    def rasterize(self, svg_content: str) -> bytes:
        """Convert SVG string to PNG bytes at 2x, then downscale.

        Args:
            svg_content: SVG markup string

        Returns:
            PNG image bytes at final output dimensions
        """
        try:
            from resvg_py import svg_to_bytes
        except ImportError as e:
            raise RuntimeError(
                "resvg-py is not installed. Run: pip install resvg-py"
            ) from e

        # Build render options
        render_kwargs: dict = {
            "svg_string": svg_content,
            "width": RENDER_WIDTH,
            "height": RENDER_HEIGHT,
        }

        # Add font directory if available
        if self.font_dir:
            render_kwargs["font_dirs"] = [self.font_dir]

        # Render SVG to PNG at 2x resolution
        png_2x = svg_to_bytes(**render_kwargs)

        # Downscale to final size using Pillow for high-quality resize
        img = Image.open(BytesIO(png_2x))
        img_resized = img.resize(
            (OUTPUT_WIDTH, OUTPUT_HEIGHT),
            Image.Resampling.LANCZOS,
        )

        # Save as optimized PNG
        output = BytesIO()
        img_resized.save(output, format="PNG", optimize=True)
        return output.getvalue()

    def rasterize_to_file(self, svg_content: str, output_path: Path) -> None:
        """Render SVG and save directly to file.

        Args:
            svg_content: SVG markup string
            output_path: Path to save the PNG file
        """
        png_bytes = self.rasterize(svg_content)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(png_bytes)
        logger.debug(f"Saved rasterized PNG to: {output_path}")


# Module-level singleton
_rasterizer: Rasterizer | None = None


def get_rasterizer() -> Rasterizer:
    """Get or create the rasterizer singleton."""
    global _rasterizer
    if _rasterizer is None:
        _rasterizer = Rasterizer()
    return _rasterizer
