"""Admin image service for viewing and managing player image assets.

Handles listing, viewing, and deleting generated player images.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.image_snapshots import PlayerImageAsset, PlayerImageSnapshot
from app.services.s3_client import s3_client

logger = logging.getLogger(__name__)


@dataclass
class ImageListResult:
    """Result of a paginated image asset list query."""

    images: list[PlayerImageAsset]
    total: int


async def list_player_images(
    db: AsyncSession,
    player_id: int,
    limit: int = 50,
    offset: int = 0,
) -> ImageListResult:
    """List all image assets for a player.

    Args:
        db: Async database session
        player_id: Player's database ID
        limit: Maximum results to return
        offset: Number of results to skip

    Returns:
        ImageListResult with images and total count
    """
    # Get total count
    count_result = await db.execute(
        select(func.count()).where(
            PlayerImageAsset.player_id == player_id  # type: ignore[arg-type]
        )
    )
    total = count_result.scalar() or 0

    # Get paginated results
    result = await db.execute(
        select(PlayerImageAsset)
        .where(PlayerImageAsset.player_id == player_id)  # type: ignore[arg-type]
        .order_by(PlayerImageAsset.generated_at.desc())  # type: ignore[union-attr, attr-defined]
        .limit(limit)
        .offset(offset)
    )
    images = list(result.scalars().all())

    return ImageListResult(images=images, total=total)


async def get_image_asset(
    db: AsyncSession,
    asset_id: int,
) -> PlayerImageAsset | None:
    """Fetch a single image asset by ID.

    Args:
        db: Async database session
        asset_id: Image asset ID

    Returns:
        PlayerImageAsset if found, None otherwise
    """
    result = await db.execute(
        select(PlayerImageAsset).where(
            PlayerImageAsset.id == asset_id  # type: ignore[arg-type]
        )
    )
    return result.scalar_one_or_none()


async def get_image_snapshot(
    db: AsyncSession,
    snapshot_id: int,
) -> PlayerImageSnapshot | None:
    """Fetch a snapshot by ID.

    Args:
        db: Async database session
        snapshot_id: Snapshot ID

    Returns:
        PlayerImageSnapshot if found, None otherwise
    """
    result = await db.execute(
        select(PlayerImageSnapshot).where(
            PlayerImageSnapshot.id == snapshot_id  # type: ignore[arg-type]
        )
    )
    return result.scalar_one_or_none()


async def delete_image_asset(
    db: AsyncSession,
    asset: PlayerImageAsset,
    delete_from_storage: bool = True,
) -> bool:
    """Delete an image asset from the database and optionally from S3.

    Args:
        db: Async database session
        asset: Image asset to delete
        delete_from_storage: Whether to also delete from S3/local storage

    Returns:
        True if deletion succeeded, False if storage deletion failed
    """
    storage_deleted = True

    # Try to delete from S3/local storage
    if delete_from_storage and asset.s3_key:
        try:
            s3_client.delete(asset.s3_key)
            logger.info(f"Deleted image from storage: {asset.s3_key}")
        except Exception as e:
            logger.warning(f"Failed to delete image from storage: {e}")
            storage_deleted = False

    # Delete from database
    await db.delete(asset)
    await db.flush()

    return storage_deleted
