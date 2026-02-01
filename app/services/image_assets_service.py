"""Helpers for resolving player image URLs from current image assets.

In production, generated images live in S3 (or an S3-compatible service). The UI
should therefore resolve image URLs from the audited `player_image_assets` table
instead of checking the local filesystem.
"""

from __future__ import annotations

from typing import Iterable

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.image_snapshots import PlayerImageAsset, PlayerImageSnapshot


async def get_current_image_url_for_player(
    db: AsyncSession,
    *,
    player_id: int,
    style: str,
) -> str | None:
    """Return the latest image URL for a single player/style, if any.

    Note: DraftGuru intentionally maintains a single canonical image per
    (player, style) by overwriting the same S3 key. Snapshot "current" state is
    therefore not required to resolve the active image; we simply return the
    most recently generated successful asset for that player/style.
    """
    stmt = (
        select(PlayerImageAsset.public_url)  # type: ignore[call-overload]
        .join(
            PlayerImageSnapshot, PlayerImageSnapshot.id == PlayerImageAsset.snapshot_id
        )
        .where(
            PlayerImageAsset.player_id == player_id,
            PlayerImageAsset.error_message.is_(None),  # type: ignore[union-attr]
            PlayerImageSnapshot.style == style,
        )
        .order_by(
            desc(PlayerImageAsset.generated_at),  # type: ignore[arg-type]
            desc(PlayerImageSnapshot.generated_at),  # type: ignore[arg-type]
            desc(PlayerImageSnapshot.id),  # type: ignore[arg-type]
        )
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def get_current_image_urls_for_players(
    db: AsyncSession,
    *,
    player_ids: Iterable[int],
    style: str,
) -> dict[int, str]:
    """Return a mapping of player_id -> latest image URL for a style."""
    ids = list(player_ids)
    if not ids:
        return {}

    stmt = (
        select(  # type: ignore[call-overload]
            PlayerImageAsset.player_id,
            PlayerImageAsset.public_url,
        )
        .join(
            PlayerImageSnapshot, PlayerImageSnapshot.id == PlayerImageAsset.snapshot_id
        )
        .where(
            PlayerImageAsset.player_id.in_(ids),  # type: ignore[attr-defined]
            PlayerImageAsset.error_message.is_(None),  # type: ignore[union-attr]
            PlayerImageSnapshot.style == style,
        )
        .distinct(PlayerImageAsset.player_id)
        .order_by(
            PlayerImageAsset.player_id,
            desc(PlayerImageAsset.generated_at),  # type: ignore[arg-type]
            desc(PlayerImageSnapshot.generated_at),  # type: ignore[arg-type]
            desc(PlayerImageSnapshot.id),  # type: ignore[arg-type]
        )
    )

    result = await db.execute(stmt)
    return {player_id: public_url for player_id, public_url in result.all()}
