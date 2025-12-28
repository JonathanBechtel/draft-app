"""Image utility functions for player photos."""

import os
from typing import Optional

# Available image styles (order = priority for fallback)
IMAGE_STYLES = ["default", "vector", "comic", "retro"]
DEFAULT_STYLE = "default"

# Base directory for player images (relative to project root)
PLAYER_IMAGES_DIR = "app/static/img/players"


def get_player_photo_url(
    player_id: int,
    slug: str,
    display_name: Optional[str] = None,
    style: Optional[str] = None,
) -> str:
    """Return photo URL for player with specified style.

    Checks for images in this order:
    1. New format: {id}_{slug}_{style}.png
    2. New format default: {id}_{slug}_default.png
    3. Legacy format: {id}_{style}.jpg
    4. Legacy format default: {id}_default.jpg
    5. Placeholder URL

    Args:
        player_id: Player's database ID (deterministic reference)
        slug: Player's URL slug for human-readable filenames
        display_name: Player's display name for placeholder text
        style: Image style variant (default, vector, comic, retro)

    Returns:
        URL string - local static path if file exists, placeholder otherwise
    """
    requested_style = style or DEFAULT_STYLE

    # Check for new format: {id}_{slug}_{style}.png
    new_path = f"{PLAYER_IMAGES_DIR}/{player_id}_{slug}_{requested_style}.png"
    if os.path.exists(new_path):
        return f"/static/img/players/{player_id}_{slug}_{requested_style}.png"

    # If specific style requested but not found, try new format default
    if style and style != DEFAULT_STYLE:
        new_default_path = f"{PLAYER_IMAGES_DIR}/{player_id}_{slug}_{DEFAULT_STYLE}.png"
        if os.path.exists(new_default_path):
            return f"/static/img/players/{player_id}_{slug}_{DEFAULT_STYLE}.png"

    # Legacy fallback: {id}_{style}.jpg
    legacy_path = f"{PLAYER_IMAGES_DIR}/{player_id}_{requested_style}.jpg"
    if os.path.exists(legacy_path):
        return f"/static/img/players/{player_id}_{requested_style}.jpg"

    # Legacy fallback default
    if style and style != DEFAULT_STYLE:
        legacy_default = f"{PLAYER_IMAGES_DIR}/{player_id}_{DEFAULT_STYLE}.jpg"
        if os.path.exists(legacy_default):
            return f"/static/img/players/{player_id}_{DEFAULT_STYLE}.jpg"

    # Fallback to placeholder
    name = display_name or f"Player {player_id}"
    return f"https://placehold.co/400x533/edf2f7/1f2937?text={name.replace(' ', '+')}"


def get_available_styles(player_id: int, slug: str) -> list[str]:
    """Return list of available image styles for a player.

    Checks both new format (.png with slug) and legacy format (.jpg).

    Args:
        player_id: Player's database ID
        slug: Player's URL slug

    Returns:
        List of style names that have corresponding image files
    """
    available = []
    for style in IMAGE_STYLES:
        # Check new format first
        new_path = f"{PLAYER_IMAGES_DIR}/{player_id}_{slug}_{style}.png"
        if os.path.exists(new_path):
            available.append(style)
            continue
        # Check legacy format
        legacy_path = f"{PLAYER_IMAGES_DIR}/{player_id}_{style}.jpg"
        if os.path.exists(legacy_path):
            available.append(style)
    return available


def get_placeholder_url(
    display_name: Optional[str] = None,
    *,
    player_id: int = 0,
    width: int = 400,
    height: int = 533,
) -> str:
    """Generate a placeholder image URL.

    Args:
        display_name: Player's display name for placeholder text
        player_id: Player's database ID (fallback for text)
        width: Placeholder width in pixels
        height: Placeholder height in pixels

    Returns:
        Placeholder URL from placehold.co
    """
    name = display_name or f"Player {player_id}"
    return (
        f"https://placehold.co/{width}x{height}/edf2f7/1f2937?text="
        f"{name.replace(' ', '+')}"
    )
