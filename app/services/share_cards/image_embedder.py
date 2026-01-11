"""Fetch player images and embed as base64 data URIs for SVG templates."""

import base64
import logging
from io import BytesIO
from typing import Optional, Tuple

import httpx
from PIL import Image

logger = logging.getLogger(__name__)

# Target size for embedded photos (at 2x for high-DPI)
PHOTO_WIDTH = 280
PHOTO_HEIGHT = 280


async def fetch_and_embed_image(
    public_url: Optional[str],
    player_name: str,
    width: int = PHOTO_WIDTH,
    height: int = PHOTO_HEIGHT,
) -> Tuple[Optional[str], bool]:
    """Fetch image from URL, resize, and convert to base64 data URI.

    Args:
        public_url: URL to fetch image from (S3 or CDN)
        player_name: Player name for fallback/logging
        width: Target width in pixels
        height: Target height in pixels

    Returns:
        Tuple of (data_uri or None, has_photo: bool)
    """
    if not public_url:
        logger.debug(f"No image URL for player: {player_name}")
        return None, False

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(public_url)
            response.raise_for_status()

        # Open and process image
        img_file = Image.open(BytesIO(response.content))
        img: Image.Image = img_file.convert("RGB")

        # Center-crop with slight top bias for faces
        img = _center_crop_with_bias(img, width, height, top_bias=0.1)

        # Convert to PNG bytes
        buffer = BytesIO()
        img.save(buffer, format="PNG", optimize=True)
        png_bytes = buffer.getvalue()

        # Encode as base64 data URI
        b64 = base64.b64encode(png_bytes).decode("ascii")
        data_uri = f"data:image/png;base64,{b64}"

        return data_uri, True

    except httpx.HTTPStatusError as e:
        logger.warning(
            f"HTTP error fetching image for {player_name}: {e.response.status_code}"
        )
        return None, False
    except httpx.RequestError as e:
        logger.warning(f"Request error fetching image for {player_name}: {e}")
        return None, False
    except Exception as e:
        logger.warning(f"Error processing image for {player_name}: {e}")
        return None, False


def _center_crop_with_bias(
    img: Image.Image,
    target_w: int,
    target_h: int,
    top_bias: float = 0.1,
) -> Image.Image:
    """Crop image to target aspect ratio with configurable vertical bias.

    Args:
        img: PIL Image to crop
        target_w: Target width
        target_h: Target height
        top_bias: Vertical bias (0.0 = center, 0.1 = slight top bias for faces)

    Returns:
        Cropped and resized PIL Image
    """
    src_w, src_h = img.size
    target_ratio = target_w / target_h
    src_ratio = src_w / src_h

    if src_ratio > target_ratio:
        # Source is wider - crop sides
        new_w = int(src_h * target_ratio)
        left = (src_w - new_w) // 2
        img = img.crop((left, 0, left + new_w, src_h))
    else:
        # Source is taller - crop top/bottom with bias
        new_h = int(src_w / target_ratio)
        bias_offset = int((src_h - new_h) * top_bias)
        top = bias_offset
        img = img.crop((0, top, src_w, top + new_h))

    return img.resize((target_w, target_h), Image.Resampling.LANCZOS)


def render_name_placeholder_svg(
    name: str,
    width: int = PHOTO_WIDTH,
    height: int = PHOTO_HEIGHT,
) -> str:
    """Generate SVG group element showing player name in a styled box.

    Used when player image is missing. Returns SVG group markup, not a data URI.

    Args:
        name: Player's display name
        width: Box width in pixels
        height: Box height in pixels

    Returns:
        SVG <g> element string
    """
    # Split name for two-line display if needed
    parts = name.split()
    cx = width // 2
    cy = height // 2

    if len(parts) > 1:
        line1 = parts[0]
        line2 = " ".join(parts[1:])
        # Truncate long names
        if len(line2) > 15:
            line2 = line2[:14] + "..."
        return f"""<g>
    <rect x="0" y="0" width="{width}" height="{height}"
          fill="#e2e8f0" stroke="#cbd5e1" stroke-width="2" rx="8"/>
    <text x="{cx}" y="{cy - 24}"
          text-anchor="middle" fill="#475569"
          font-family="'Azeret Mono', monospace" font-size="32" font-weight="600">
        {_escape_xml(line1)}
    </text>
    <text x="{cx}" y="{cy + 24}"
          text-anchor="middle" fill="#475569"
          font-family="'Azeret Mono', monospace" font-size="32" font-weight="600">
        {_escape_xml(line2)}
    </text>
</g>"""
    else:
        return f"""<g>
    <rect x="0" y="0" width="{width}" height="{height}"
          fill="#e2e8f0" stroke="#cbd5e1" stroke-width="2" rx="8"/>
    <text x="{cx}" y="{cy}"
          text-anchor="middle" dominant-baseline="middle" fill="#475569"
          font-family="'Azeret Mono', monospace" font-size="36" font-weight="600">
        {_escape_xml(name)}
    </text>
</g>"""


def _escape_xml(text: str) -> str:
    """Escape special XML characters."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )
