"""Admin image service for browsing and managing generated player images.

Provides query functions for listing, filtering, and managing PlayerImageAsset records
with their associated PlayerImageSnapshot and PlayerMaster data.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.schemas.image_snapshots import (
    PendingImagePreview,
    PlayerImageAsset,
    PlayerImageSnapshot,
)
from app.schemas.players_master import PlayerMaster
from app.services.image_generation import PreviewResult
from app.services.s3_client import s3_client
from app.utils.images import get_player_image_url, get_s3_image_base_url

logger = logging.getLogger(__name__)

# Default preview TTL (24 hours)
PREVIEW_TTL_HOURS = 24


@dataclass
class ImageAssetInfo:
    """Image asset with snapshot context for admin display."""

    id: int
    player_id: int
    player_name: str
    player_slug: str
    style: str
    public_url: str
    display_url: str  # S3-first constructed URL for display
    generated_at: datetime
    is_current: bool
    snapshot_id: int
    snapshot_version: int
    file_size_bytes: int | None
    error_message: str | None
    used_likeness_ref: bool
    reference_image_url: str | None


@dataclass
class ImageListResult:
    """Paginated image list with filter metadata."""

    images: list[ImageAssetInfo]
    total: int
    styles: list[str]
    draft_years: list[int]


async def list_images(
    db: AsyncSession,
    style: str | None = None,
    player_id: int | None = None,
    draft_year: int | None = None,
    q: str | None = None,
    current_only: bool = False,
    include_errors: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> ImageListResult:
    """List images with filters and pagination.

    Args:
        db: Async database session
        style: Filter by image style (e.g., 'default', 'vector', 'comic')
        player_id: Filter by specific player
        draft_year: Filter by player draft year
        q: Search query for player name
        current_only: Only show images from current snapshots
        include_errors: Include images with generation errors
        limit: Maximum results to return
        offset: Number of results to skip

    Returns:
        ImageListResult with images, total count, and available filter options
    """
    # Build join conditions
    snapshot_join = PlayerImageAsset.snapshot_id == PlayerImageSnapshot.id
    player_join = PlayerImageAsset.player_id == PlayerMaster.id

    # Base query joining assets with snapshots and players
    query = (
        select(PlayerImageAsset, PlayerImageSnapshot, PlayerMaster)
        .join(PlayerImageSnapshot, snapshot_join)  # type: ignore[arg-type]
        .join(PlayerMaster, player_join)  # type: ignore[arg-type]
        .order_by(PlayerImageAsset.generated_at.desc())  # type: ignore[union-attr, attr-defined]
    )

    count_query = (
        select(func.count(PlayerImageAsset.id))  # type: ignore[arg-type]
        .join(PlayerImageSnapshot, snapshot_join)  # type: ignore[arg-type]
        .join(PlayerMaster, player_join)  # type: ignore[arg-type]
    )

    # Apply player name search
    if q and q.strip():
        search_term = f"%{q.strip()}%"
        name_filter = PlayerMaster.display_name.ilike(search_term)  # type: ignore[union-attr]
        query = query.where(name_filter)
        count_query = count_query.where(name_filter)

    # Apply filters
    if style:
        style_filter = PlayerImageSnapshot.style == style
        query = query.where(style_filter)  # type: ignore[arg-type]
        count_query = count_query.where(style_filter)  # type: ignore[arg-type]

    if player_id is not None:
        player_filter = PlayerImageAsset.player_id == player_id
        query = query.where(player_filter)  # type: ignore[arg-type]
        count_query = count_query.where(player_filter)  # type: ignore[arg-type]

    if draft_year is not None:
        year_filter = PlayerMaster.draft_year == draft_year
        query = query.where(year_filter)  # type: ignore[arg-type]
        count_query = count_query.where(year_filter)  # type: ignore[arg-type]

    if current_only:
        current_filter = PlayerImageSnapshot.is_current == True  # noqa: E712
        query = query.where(current_filter)  # type: ignore[arg-type]
        count_query = count_query.where(current_filter)  # type: ignore[arg-type]

    if not include_errors:
        no_error_filter = PlayerImageAsset.error_message.is_(None)  # type: ignore[union-attr]
        query = query.where(no_error_filter)
        count_query = count_query.where(no_error_filter)

    # Get total count
    total = await db.scalar(count_query)
    total = total or 0

    # Apply pagination
    query = query.limit(limit).offset(offset)

    result = await db.execute(query)
    rows = result.all()

    # Build ImageAssetInfo list
    base_url = get_s3_image_base_url()
    images: list[ImageAssetInfo] = []
    for asset, snapshot, player in rows:
        # Construct display URL using S3-first approach with cache-busting
        base_display_url = get_player_image_url(
            player_id=asset.player_id,
            slug=player.slug or "",
            style=snapshot.style,
            base_url=base_url,
        )
        # Add cache-busting based on generated_at timestamp
        cache_bust = int(asset.generated_at.timestamp())
        display_url = f"{base_display_url}?v={cache_bust}"
        images.append(
            ImageAssetInfo(
                id=asset.id,
                player_id=asset.player_id,
                player_name=player.display_name or "",
                player_slug=player.slug or "",
                style=snapshot.style,
                public_url=asset.public_url,
                display_url=display_url,
                generated_at=asset.generated_at,
                is_current=snapshot.is_current,
                snapshot_id=snapshot.id,
                snapshot_version=snapshot.version,
                file_size_bytes=asset.file_size_bytes,
                error_message=asset.error_message,
                used_likeness_ref=asset.used_likeness_ref,
                reference_image_url=asset.reference_image_url,
            )
        )

    # Get distinct styles for filter dropdown
    styles_result = await db.execute(
        select(PlayerImageSnapshot.style)  # type: ignore[call-overload]
        .distinct()
        .order_by(PlayerImageSnapshot.style)  # type: ignore[arg-type]
    )
    styles = list(styles_result.scalars().all())

    # Get distinct draft years for filter dropdown
    years_join = PlayerMaster.id == PlayerImageAsset.player_id
    years_query = (
        select(PlayerMaster.draft_year)  # type: ignore[call-overload]
        .join(PlayerImageAsset, years_join)  # type: ignore[arg-type]
        .where(PlayerMaster.draft_year.isnot(None))  # type: ignore[union-attr]
        .distinct()
        .order_by(PlayerMaster.draft_year.desc())  # type: ignore[union-attr]
    )
    years_result = await db.execute(years_query)
    draft_years = [y for y in years_result.scalars().all() if y is not None]

    return ImageListResult(
        images=images,
        total=total,
        styles=styles,
        draft_years=draft_years,
    )


async def get_images_for_player(
    db: AsyncSession,
    player_id: int,
    current_only: bool = False,
) -> list[ImageAssetInfo]:
    """Get all images for a specific player.

    Args:
        db: Async database session
        player_id: Player's database ID
        current_only: Only return images from current snapshots

    Returns:
        List of ImageAssetInfo for the player
    """
    # Build join conditions
    snapshot_join = PlayerImageAsset.snapshot_id == PlayerImageSnapshot.id
    player_join = PlayerImageAsset.player_id == PlayerMaster.id

    query = (
        select(PlayerImageAsset, PlayerImageSnapshot, PlayerMaster)
        .join(PlayerImageSnapshot, snapshot_join)  # type: ignore[arg-type]
        .join(PlayerMaster, player_join)  # type: ignore[arg-type]
        .where(PlayerImageAsset.player_id == player_id)  # type: ignore[arg-type]
        .order_by(PlayerImageSnapshot.style)  # type: ignore[arg-type]
    )

    if current_only:
        current_filter = PlayerImageSnapshot.is_current == True  # noqa: E712
        query = query.where(current_filter)  # type: ignore[arg-type]

    result = await db.execute(query)
    rows = result.all()

    base_url = get_s3_image_base_url()
    images: list[ImageAssetInfo] = []
    for asset, snapshot, player in rows:
        # Construct display URL using S3-first approach with cache-busting
        base_display_url = get_player_image_url(
            player_id=asset.player_id,
            slug=player.slug or "",
            style=snapshot.style,
            base_url=base_url,
        )
        # Add cache-busting based on generated_at timestamp
        cache_bust = int(asset.generated_at.timestamp())
        display_url = f"{base_display_url}?v={cache_bust}"
        images.append(
            ImageAssetInfo(
                id=asset.id,
                player_id=asset.player_id,
                player_name=player.display_name or "",
                player_slug=player.slug or "",
                style=snapshot.style,
                public_url=asset.public_url,
                display_url=display_url,
                generated_at=asset.generated_at,
                is_current=snapshot.is_current,
                snapshot_id=snapshot.id,
                snapshot_version=snapshot.version,
                file_size_bytes=asset.file_size_bytes,
                error_message=asset.error_message,
                used_likeness_ref=asset.used_likeness_ref,
                reference_image_url=asset.reference_image_url,
            )
        )

    return images


async def get_image_by_id(
    db: AsyncSession,
    asset_id: int,
) -> ImageAssetInfo | None:
    """Get a single image asset by ID.

    Args:
        db: Async database session
        asset_id: The image asset's database ID

    Returns:
        ImageAssetInfo if found, None otherwise
    """
    # Build join conditions
    snapshot_join = PlayerImageAsset.snapshot_id == PlayerImageSnapshot.id
    player_join = PlayerImageAsset.player_id == PlayerMaster.id

    query = (
        select(PlayerImageAsset, PlayerImageSnapshot, PlayerMaster)
        .join(PlayerImageSnapshot, snapshot_join)  # type: ignore[arg-type]
        .join(PlayerMaster, player_join)  # type: ignore[arg-type]
        .where(PlayerImageAsset.id == asset_id)  # type: ignore[arg-type]
    )

    result = await db.execute(query)
    row = result.one_or_none()

    if row is None:
        return None

    asset, snapshot, player = row
    # Construct display URL using S3-first approach with cache-busting
    base_display_url = get_player_image_url(
        player_id=asset.player_id,
        slug=player.slug or "",
        style=snapshot.style,
        base_url=get_s3_image_base_url(),
    )
    # Add cache-busting based on generated_at timestamp
    cache_bust = int(asset.generated_at.timestamp())
    display_url = f"{base_display_url}?v={cache_bust}"
    return ImageAssetInfo(
        id=asset.id,
        player_id=asset.player_id,
        player_name=player.display_name or "",
        player_slug=player.slug or "",
        style=snapshot.style,
        public_url=asset.public_url,
        display_url=display_url,
        generated_at=asset.generated_at,
        is_current=snapshot.is_current,
        snapshot_id=snapshot.id,
        snapshot_version=snapshot.version,
        file_size_bytes=asset.file_size_bytes,
        error_message=asset.error_message,
        used_likeness_ref=asset.used_likeness_ref,
        reference_image_url=asset.reference_image_url,
    )


async def delete_image(
    db: AsyncSession,
    asset_id: int,
) -> bool:
    """Delete an image asset from the database.

    Note: This only removes the database record. S3 cleanup should be handled
    separately if needed.

    Args:
        db: Async database session
        asset_id: The image asset's database ID

    Returns:
        True if deleted, False if not found
    """
    result = await db.execute(
        select(PlayerImageAsset).where(
            PlayerImageAsset.id == asset_id  # type: ignore[arg-type]
        )
    )
    asset = result.scalar_one_or_none()

    if asset is None:
        return False

    await db.delete(asset)
    await db.flush()
    return True


@dataclass
class PreviewInfo:
    """Preview image with context for admin display."""

    id: int
    player_id: int
    player_name: str
    player_slug: str
    style: str
    source_asset_id: int | None
    image_data_base64: str
    file_size_bytes: int
    user_prompt: str
    likeness_description: str | None
    used_likeness_ref: bool
    reference_image_url: str | None
    generation_time_sec: float
    created_at: datetime
    expires_at: datetime
    # Current image info (if source asset exists)
    current_image_url: str | None


async def create_preview(
    db: AsyncSession,
    player_id: int,
    source_asset_id: int | None,
    style: str,
    preview_result: PreviewResult,
) -> PendingImagePreview:
    """Create a pending preview record from a generation result.

    Args:
        db: Async database session
        player_id: Player's database ID
        source_asset_id: Original asset ID being regenerated (if any)
        style: Image style
        preview_result: Result from image generation

    Returns:
        Created PendingImagePreview record
    """
    logger.info(
        f"create_preview: player_id={player_id}, source_asset_id={source_asset_id}, style={style}"
    )
    now = datetime.utcnow()
    expires_at = now + timedelta(hours=PREVIEW_TTL_HOURS)

    preview = PendingImagePreview(
        player_id=player_id,
        source_asset_id=source_asset_id,
        style=style,
        image_data_base64=base64.b64encode(preview_result.image_data).decode("utf-8"),
        file_size_bytes=len(preview_result.image_data),
        user_prompt=preview_result.user_prompt,
        likeness_description=preview_result.likeness_description,
        used_likeness_ref=preview_result.used_likeness_ref,
        reference_image_url=preview_result.reference_image_url,
        generation_time_sec=preview_result.generation_time_sec,
        created_at=now,
        expires_at=expires_at,
    )

    db.add(preview)
    await db.flush()
    await db.refresh(preview)

    return preview


async def get_preview_by_id(
    db: AsyncSession,
    preview_id: int,
) -> PreviewInfo | None:
    """Get a preview with player context.

    Args:
        db: Async database session
        preview_id: The preview's database ID

    Returns:
        PreviewInfo if found, None otherwise
    """
    query = (
        select(PendingImagePreview, PlayerMaster)
        .join(
            PlayerMaster,
            PendingImagePreview.player_id == PlayerMaster.id,  # type: ignore[arg-type]
        )
        .where(PendingImagePreview.id == preview_id)  # type: ignore[arg-type]
    )

    result = await db.execute(query)
    row = result.one_or_none()

    if row is None:
        return None

    preview, player = row

    # Get current image URL if source asset exists
    current_image_url: str | None = None
    if preview.source_asset_id is not None:
        asset_result = await db.execute(
            select(PlayerImageAsset).where(
                PlayerImageAsset.id == preview.source_asset_id  # type: ignore[arg-type]
            )
        )
        asset = asset_result.scalar_one_or_none()
        if asset:
            current_image_url = get_player_image_url(
                player_id=asset.player_id,
                slug=player.slug or "",
                style=preview.style,
                base_url=get_s3_image_base_url(),
            )

    return PreviewInfo(
        id=preview.id,  # type: ignore[arg-type]
        player_id=preview.player_id,
        player_name=player.display_name or "",
        player_slug=player.slug or "",
        style=preview.style,
        source_asset_id=preview.source_asset_id,
        image_data_base64=preview.image_data_base64,
        file_size_bytes=preview.file_size_bytes,
        user_prompt=preview.user_prompt,
        likeness_description=preview.likeness_description,
        used_likeness_ref=preview.used_likeness_ref,
        reference_image_url=preview.reference_image_url,
        generation_time_sec=preview.generation_time_sec,
        created_at=preview.created_at,
        expires_at=preview.expires_at,
        current_image_url=current_image_url,
    )


async def delete_preview(
    db: AsyncSession,
    preview_id: int,
) -> bool:
    """Delete a preview record.

    Args:
        db: Async database session
        preview_id: The preview's database ID

    Returns:
        True if deleted, False if not found
    """
    result = await db.execute(
        select(PendingImagePreview).where(
            PendingImagePreview.id == preview_id  # type: ignore[arg-type]
        )
    )
    preview = result.scalar_one_or_none()

    if preview is None:
        return False

    await db.delete(preview)
    await db.flush()
    return True


async def approve_preview(
    db: AsyncSession,
    preview_id: int,
) -> PlayerImageAsset | None:
    """Approve a preview: upload to S3 and create/update asset record.

    This function:
    1. Retrieves the preview
    2. Uploads the image to S3
    3. Creates a new PlayerImageAsset (or updates existing if source_asset_id set)
    4. Deletes the preview

    Args:
        db: Async database session
        preview_id: The preview's database ID

    Returns:
        Created/updated PlayerImageAsset if successful, None if preview not found
    """
    # Get preview with player info
    query = (
        select(PendingImagePreview, PlayerMaster)
        .join(
            PlayerMaster,
            PendingImagePreview.player_id == PlayerMaster.id,  # type: ignore[arg-type]
        )
        .where(PendingImagePreview.id == preview_id)  # type: ignore[arg-type]
    )

    result = await db.execute(query)
    row = result.one_or_none()

    if row is None:
        return None

    preview, player = row

    logger.info(
        f"approve_preview: preview_id={preview_id}, "
        f"source_asset_id={preview.source_asset_id}, "
        f"player_id={preview.player_id}"
    )

    # Decode image data
    image_data = base64.b64decode(preview.image_data_base64)

    # Build S3 key
    s3_key = f"players/{preview.player_id}_{player.slug or str(preview.player_id)}_{preview.style}.png"

    logger.info(f"approve_preview: uploading to s3_key={s3_key}")

    # Upload to S3
    base_public_url = s3_client.upload(
        s3_key,
        image_data,
        content_type="image/png",
        metadata={
            "player_id": str(preview.player_id),
            "player_slug": player.slug or "",
            "style": preview.style,
        },
    )

    # Add cache-busting timestamp to force browser/CDN refresh
    cache_bust = int(datetime.utcnow().timestamp())
    public_url = f"{base_public_url}?v={cache_bust}"

    # Get or create snapshot for this style
    # For admin regeneration, we'll reuse the current snapshot if one exists
    snapshot_result = await db.execute(
        select(PlayerImageSnapshot)
        .where(PlayerImageSnapshot.style == preview.style)  # type: ignore[arg-type]
        .where(PlayerImageSnapshot.is_current == True)  # type: ignore[arg-type] # noqa: E712
        .order_by(PlayerImageSnapshot.generated_at.desc())  # type: ignore[union-attr, attr-defined]
        .limit(1)
    )
    snapshot = snapshot_result.scalar_one_or_none()

    if snapshot is None:
        # Create a new snapshot for admin-generated images
        from app.services.image_generation import image_generation_service

        snapshot = PlayerImageSnapshot(
            run_key=f"admin_regen_{datetime.utcnow().strftime('%Y%m%d')}",
            version=1,
            is_current=True,
            style=preview.style,
            cohort="global_scope",  # type: ignore[arg-type]
            population_size=1,
            success_count=1,
            failure_count=0,
            image_size=settings.image_gen_size,
            system_prompt=image_generation_service.get_system_prompt(),
            system_prompt_version="default",
        )
        db.add(snapshot)
        await db.flush()
        await db.refresh(snapshot)

    snapshot_id = snapshot.id
    if snapshot_id is None:
        raise ValueError("snapshot.id should not be None after flush")

    # Check if we're updating an existing asset by source_asset_id
    if preview.source_asset_id is not None:
        logger.info(
            f"approve_preview: looking for existing asset with id={preview.source_asset_id}"
        )
        asset_result = await db.execute(
            select(PlayerImageAsset).where(
                PlayerImageAsset.id == preview.source_asset_id  # type: ignore[arg-type]
            )
        )
        existing_asset = asset_result.scalar_one_or_none()

        if existing_asset:
            logger.info(
                "approve_preview: found existing asset by source_asset_id, updating it"
            )
            # Update existing asset
            existing_asset.snapshot_id = snapshot_id
            existing_asset.s3_key = s3_key
            existing_asset.s3_bucket = settings.s3_bucket_name
            existing_asset.public_url = public_url
            existing_asset.file_size_bytes = len(image_data)
            existing_asset.user_prompt = preview.user_prompt
            existing_asset.likeness_description = preview.likeness_description
            existing_asset.used_likeness_ref = preview.used_likeness_ref
            existing_asset.reference_image_url = preview.reference_image_url
            existing_asset.generated_at = datetime.utcnow()
            existing_asset.generation_time_sec = preview.generation_time_sec
            existing_asset.error_message = None

            # Delete preview
            await db.delete(preview)
            await db.flush()
            await db.refresh(existing_asset)

            return existing_asset

    # Check for any existing asset with the same s3_key (player+style combo)
    # This handles the case where source_asset_id wasn't set but an asset exists
    logger.info(f"approve_preview: checking for existing asset with s3_key={s3_key}")
    existing_by_key_result = await db.execute(
        select(PlayerImageAsset).where(
            PlayerImageAsset.s3_key == s3_key  # type: ignore[arg-type]
        )
    )
    existing_by_key = existing_by_key_result.scalar_one_or_none()

    if existing_by_key:
        logger.info(
            f"approve_preview: found existing asset by s3_key (id={existing_by_key.id}), updating it"
        )
        # Update existing asset instead of creating duplicate
        existing_by_key.snapshot_id = snapshot_id
        existing_by_key.s3_bucket = settings.s3_bucket_name
        existing_by_key.public_url = public_url
        existing_by_key.file_size_bytes = len(image_data)
        existing_by_key.user_prompt = preview.user_prompt
        existing_by_key.likeness_description = preview.likeness_description
        existing_by_key.used_likeness_ref = preview.used_likeness_ref
        existing_by_key.reference_image_url = preview.reference_image_url
        existing_by_key.generated_at = datetime.utcnow()
        existing_by_key.generation_time_sec = preview.generation_time_sec
        existing_by_key.error_message = None

        # Delete preview
        await db.delete(preview)
        await db.flush()
        await db.refresh(existing_by_key)

        return existing_by_key

    # Create new asset only if no existing asset found
    logger.info("approve_preview: creating new asset (no existing asset found)")
    asset = PlayerImageAsset(
        snapshot_id=snapshot_id,
        player_id=preview.player_id,
        s3_key=s3_key,
        s3_bucket=settings.s3_bucket_name,
        public_url=public_url,
        file_size_bytes=len(image_data),
        user_prompt=preview.user_prompt,
        likeness_description=preview.likeness_description,
        used_likeness_ref=preview.used_likeness_ref,
        reference_image_url=preview.reference_image_url,
        generated_at=datetime.utcnow(),
        generation_time_sec=preview.generation_time_sec,
    )
    db.add(asset)

    # Delete preview
    await db.delete(preview)
    await db.flush()
    await db.refresh(asset)

    return asset


async def get_player_by_id(
    db: AsyncSession,
    player_id: int,
) -> PlayerMaster | None:
    """Get a player by ID.

    Args:
        db: Async database session
        player_id: Player's database ID

    Returns:
        PlayerMaster if found, None otherwise
    """
    result = await db.execute(
        select(PlayerMaster).where(
            PlayerMaster.id == player_id  # type: ignore[arg-type]
        )
    )
    return result.scalar_one_or_none()
