"""Lookup helpers for college school logos.

Resolves a player's free-text school name (``PlayerMaster.school``) to the
``logo_url`` stored on ``college_schools``. The full table is small
(~500 rows) and logos change rarely, so we cache the name → URL map at
module scope and load it lazily on first access.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.college_schools import CollegeSchool

_cache: Optional[dict[str, str]] = None


async def _load_cache(db: AsyncSession) -> dict[str, str]:
    """Load and return the name → logo_url map, populating the cache once."""
    global _cache
    if _cache is not None:
        return _cache
    result = await db.execute(
        select(CollegeSchool.name, CollegeSchool.logo_url).where(  # type: ignore[call-overload]
            CollegeSchool.logo_url.is_not(None)  # type: ignore[union-attr]
        )
    )
    _cache = {name: url for name, url in result.all() if name and url}
    return _cache


async def get_logo_url_for_school(
    db: AsyncSession, school: Optional[str]
) -> Optional[str]:
    """Return the logo URL for a school name, or ``None`` if no match.

    Args:
        db: Active async session.
        school: Free-text school name as stored on ``PlayerMaster.school``.

    Returns:
        The public logo URL, or ``None`` if the school is unset or has no
        registered logo.
    """
    if not school:
        return None
    cache = await _load_cache(db)
    return cache.get(school)


async def get_logo_urls_for_schools(
    db: AsyncSession, schools: list[Optional[str]]
) -> dict[str, str]:
    """Return a ``{school_name: logo_url}`` map for the given schools.

    Schools that are ``None`` or have no registered logo are omitted from
    the result. Useful for batch-resolving logos when rendering tables.
    """
    cache = await _load_cache(db)
    return {s: cache[s] for s in schools if s and s in cache}


def clear_cache() -> None:
    """Reset the in-process cache.

    Intended for tests; production callers should rely on process restarts
    to pick up new logos.
    """
    global _cache
    _cache = None
