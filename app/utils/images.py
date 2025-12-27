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
    display_name: Optional[str] = None,
    style: Optional[str] = None,
) -> str:
    """Return photo URL for player with specified style.

    Args:
        player_id: Player's database ID (deterministic reference)
        display_name: Player's display name for placeholder text
        style: Image style variant (default, vector, comic, retro)

    Returns:
        URL string - local static path if file exists, placeholder otherwise
    """
    requested_style = style or DEFAULT_STYLE

    # Check for requested style
    local_path = f"{PLAYER_IMAGES_DIR}/{player_id}_{requested_style}.jpg"
    if os.path.exists(local_path):
        return f"/static/img/players/{player_id}_{requested_style}.jpg"

    # If specific style requested but not found, fallback to default
    if style and style != DEFAULT_STYLE:
        default_path = f"{PLAYER_IMAGES_DIR}/{player_id}_{DEFAULT_STYLE}.jpg"
        if os.path.exists(default_path):
            return f"/static/img/players/{player_id}_{DEFAULT_STYLE}.jpg"

    # Fallback to placeholder
    name = display_name or f"Player {player_id}"
    return f"https://placehold.co/400x533/edf2f7/1f2937?text={name.replace(' ', '+')}"


def get_available_styles(player_id: int) -> list[str]:
    """Return list of available image styles for a player.

    Useful for UI to show which styles are available.

    Args:
        player_id: Player's database ID

    Returns:
        List of style names that have corresponding image files
    """
    available = []
    for style in IMAGE_STYLES:
        path = f"{PLAYER_IMAGES_DIR}/{player_id}_{style}.jpg"
        if os.path.exists(path):
            available.append(style)
    return available
