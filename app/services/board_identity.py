"""Identity-guarded resolution helpers for unresolved board entries."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.boards import Board, BoardEntry, ResolutionMethod
from app.services.player_identity_guard import (
    build_variant_identity_index,
    resolve_variant_identity_match,
)


class BoardIdentityReviewError(Exception):
    """Raised when a board entry name needs human identity review."""


async def resolve_variant_identity_for_entry(
    db: AsyncSession,
    *,
    entry: BoardEntry,
    board: Board,
    translate_integrity_error: Callable[[IntegrityError], None],
) -> bool:
    """Reuse a safe canonical identity for ``entry`` when one exists.

    Returns ``True`` after resolving the entry, ``False`` when no variant
    identity matches and the caller should continue with stub creation.
    Suffix mismatches and collisions raise ``BoardIdentityReviewError``.
    """
    if not entry.raw_name:
        return False

    identity_index = await build_variant_identity_index(db)
    identity = resolve_variant_identity_match(
        entry.raw_name,
        identity_index.matches_for(entry.raw_name),
    )
    if identity.status in {"exact", "alias"}:
        if identity.player_id is None:
            raise BoardIdentityReviewError(
                "Identity guard returned a match without a player id."
            )
        entry.player_id = identity.player_id
        entry.resolution_method = (
            ResolutionMethod.EXACT
            if identity.status == "exact"
            else ResolutionMethod.ALIAS
        )
        board.updated_at = datetime.utcnow()
        try:
            await db.flush()
        except IntegrityError as exc:
            translate_integrity_error(exc)
        return True
    if identity.status == "ambiguous":
        raise BoardIdentityReviewError(
            f"'{entry.raw_name}' matches multiple players; resolve it "
            "manually before creating a stub."
        )
    if identity.status == "suffix_mismatch":
        raise BoardIdentityReviewError(
            f"'{entry.raw_name}' differs by suffix from an existing player; "
            "resolve it manually before creating a stub."
        )
    return False
