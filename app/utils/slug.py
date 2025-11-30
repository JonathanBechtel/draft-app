"""Slug generation utilities for player URLs."""

import re
import unicodedata
from typing import TYPE_CHECKING, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    pass


def generate_slug(name: str) -> str:
    """Convert a display name to a URL-safe slug.

    Args:
        name: The display name to convert (e.g., "Cooper Flagg")

    Returns:
        URL-safe slug (e.g., "cooper-flagg")
    """
    if not name:
        return ""

    # Normalize unicode characters (é -> e, etc.)
    normalized = unicodedata.normalize("NFKD", name)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")

    # Convert to lowercase
    lower = ascii_text.lower()

    # Replace spaces and underscores with hyphens
    hyphenated = re.sub(r"[\s_]+", "-", lower)

    # Remove any character that isn't alphanumeric or hyphen
    cleaned = re.sub(r"[^a-z0-9-]", "", hyphenated)

    # Collapse multiple hyphens into one
    collapsed = re.sub(r"-+", "-", cleaned)

    # Strip leading/trailing hyphens
    return collapsed.strip("-")


def _base_slug(name: str) -> str:
    """Return a normalized base slug or a safe fallback."""
    base_slug = generate_slug(name)
    return base_slug or "player"


async def generate_unique_slug(
    name: str,
    db: AsyncSession,
    exclude_id: Optional[int] = None,
) -> str:
    """Generate a unique slug, appending numeric suffix if needed.

    Args:
        name: The display name to convert
        db: Database session for checking uniqueness
        exclude_id: Player ID to exclude from collision check (for updates)

    Returns:
        Unique slug (e.g., "john-smith" or "john-smith-2")
    """
    # Lazy import to avoid circular dependency
    from app.schemas.players_master import PlayerMaster

    base_slug = _base_slug(name)

    # Check if base slug is available
    candidate = base_slug
    suffix = 1

    while True:
        query = select(PlayerMaster.id).where(PlayerMaster.slug == candidate)
        if exclude_id is not None:
            query = query.where(PlayerMaster.id != exclude_id)

        result = await db.execute(query)
        existing = result.scalar_one_or_none()

        if existing is None:
            return candidate

        # Slug taken, try next suffix
        suffix += 1
        candidate = f"{base_slug}-{suffix}"


def generate_unique_slug_from_connection(
    name: str,
    connection,
    exclude_id: Optional[int] = None,
) -> str:
    """Generate a unique slug using a synchronous SQLAlchemy connection."""
    # Lazy import to avoid circular dependency
    from app.schemas.players_master import PlayerMaster

    base_slug = _base_slug(name)
    candidate = base_slug
    suffix = 1

    while True:
        query = select(PlayerMaster.id).where(PlayerMaster.slug == candidate)
        if exclude_id is not None:
            query = query.where(PlayerMaster.id != exclude_id)

        result = connection.execute(query)
        existing = result.scalar_one_or_none()
        if existing is None:
            return candidate

        suffix += 1
        candidate = f"{base_slug}-{suffix}"


def generate_slug_sync(name: str, existing_slugs: set[str]) -> str:
    """Synchronous slug generation with collision handling for migrations.

    Args:
        name: The display name to convert
        existing_slugs: Set of already-used slugs

    Returns:
        Unique slug not in existing_slugs
    """
    base_slug = _base_slug(name)

    candidate = base_slug
    suffix = 1

    while candidate in existing_slugs:
        suffix += 1
        candidate = f"{base_slug}-{suffix}"

    return candidate
