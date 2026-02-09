"""Player mention resolution service.

Resolves player name strings to database IDs by checking display_name,
aliases, and optionally creating stub records for unknown players.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.player_aliases import PlayerAlias
from app.schemas.players_master import PlayerMaster

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PlayerMatch:
    """Result of resolving a player name to a database record."""

    player_id: int
    display_name: str
    matched_via: str  # "display_name", "alias", or "stub_created"


def split_name(full_name: str) -> tuple[str, Optional[str]]:
    """Split a full name into (first_name, last_name).

    Args:
        full_name: Full player name string (e.g. "Cooper Flagg")

    Returns:
        Tuple of (first_name, last_name). last_name is None for single-word names.
    """
    parts = full_name.strip().split()
    if len(parts) == 0:
        return ("", None)
    if len(parts) == 1:
        return (parts[0], None)
    return (parts[0], " ".join(parts[1:]))


async def _resolve_iter(
    db: AsyncSession,
    names: list[str],
    draft_year: Optional[int],
    create_stubs: bool,
) -> list[tuple[str, PlayerMatch]]:
    """Core matching loop shared by the public resolve functions.

    Returns:
        List of (input_name, PlayerMatch) tuples, deduplicated by player_id.
    """
    results: list[tuple[str, PlayerMatch]] = []
    seen_ids: set[int] = set()

    for raw_name in names:
        name = raw_name.strip()
        if not name:
            continue

        match = await _match_display_name(db, name)
        if match is None:
            match = await _match_alias(db, name)
        if match is None and create_stubs:
            match = await _create_stub_player(db, name, draft_year=draft_year)

        if match is not None and match.player_id not in seen_ids:
            seen_ids.add(match.player_id)
            results.append((name, match))

    return results


async def resolve_player_names(
    db: AsyncSession,
    names: list[str],
    draft_year: Optional[int] = None,
    create_stubs: bool = True,
) -> list[PlayerMatch]:
    """Resolve a list of player name strings to database records.

    For each name, checks in order:
      1. PlayerMaster.display_name (case-insensitive exact match)
      2. PlayerAlias.full_name (case-insensitive exact match)
      3. Creates a stub PlayerMaster if create_stubs=True

    Args:
        db: Async database session (caller manages transaction)
        names: List of player name strings to resolve
        draft_year: Optional draft year to set on created stubs
        create_stubs: Whether to create stub records for unmatched names

    Returns:
        List of PlayerMatch results (one per successfully resolved name)
    """
    if not names:
        return []
    pairs = await _resolve_iter(db, names, draft_year, create_stubs)
    return [match for _, match in pairs]


async def resolve_player_names_as_map(
    db: AsyncSession,
    names: list[str],
    draft_year: Optional[int] = None,
    create_stubs: bool = True,
) -> dict[str, int]:
    """Resolve player name strings to a map of input_name -> player_id.

    Unlike resolve_player_names(), this maps each *input* name (lowered) to
    its resolved player_id, ensuring alias-matched names are correctly keyed
    by the caller's original string rather than the canonical display_name.

    Args:
        db: Async database session (caller manages transaction)
        names: List of player name strings to resolve
        draft_year: Optional draft year to set on created stubs
        create_stubs: Whether to create stub records for unmatched names

    Returns:
        Dict mapping lowered input name -> player_id
    """
    if not names:
        return {}
    pairs = await _resolve_iter(db, names, draft_year, create_stubs)
    return {input_name.lower(): match.player_id for input_name, match in pairs}


async def _match_display_name(db: AsyncSession, name: str) -> Optional[PlayerMatch]:
    """Try to match a name against PlayerMaster.display_name (case-insensitive)."""
    stmt = select(PlayerMaster.id, PlayerMaster.display_name).where(  # type: ignore[call-overload]
        func.lower(PlayerMaster.display_name) == name.lower()  # type: ignore[arg-type]
    )
    row = (await db.execute(stmt)).first()
    if row is not None:
        return PlayerMatch(
            player_id=row[0],  # type: ignore[arg-type]
            display_name=row[1] or name,  # type: ignore[arg-type]
            matched_via="display_name",
        )
    return None


async def _match_alias(db: AsyncSession, name: str) -> Optional[PlayerMatch]:
    """Try to match a name against PlayerAlias.full_name (case-insensitive)."""
    stmt = (
        select(PlayerMaster.id, PlayerMaster.display_name)  # type: ignore[call-overload]
        .join(PlayerAlias, PlayerAlias.player_id == PlayerMaster.id)  # type: ignore[arg-type]
        .where(func.lower(PlayerAlias.full_name) == name.lower())  # type: ignore[arg-type]
    )
    row = (await db.execute(stmt)).first()
    if row is not None:
        return PlayerMatch(
            player_id=row[0],  # type: ignore[arg-type]
            display_name=row[1] or name,  # type: ignore[arg-type]
            matched_via="alias",
        )
    return None


async def _create_stub_player(
    db: AsyncSession,
    full_name: str,
    draft_year: Optional[int] = None,
) -> Optional[PlayerMatch]:
    """Create a minimal stub PlayerMaster record for an unknown player.

    Args:
        db: Async database session
        full_name: Full player name
        draft_year: Optional draft year

    Returns:
        PlayerMatch if created successfully, None otherwise
    """
    first_name, last_name = split_name(full_name)
    display_name = full_name.strip()

    player = PlayerMaster(
        first_name=first_name or None,
        last_name=last_name,
        display_name=display_name,
        draft_year=draft_year,
        is_stub=True,
    )
    db.add(player)
    await db.flush()
    logger.info(f"Created stub player: {display_name} (id={player.id})")
    return PlayerMatch(
        player_id=player.id,  # type: ignore[arg-type]
        display_name=display_name,
        matched_via="stub_created",
    )
