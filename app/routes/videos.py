"""Film-room API routes."""

from datetime import UTC, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.videos import (
    FilmSearchSuggestionsResponse,
    ManualVideoAddRequest,
    VideoFeedResponse,
    VideoIngestionResult,
    YouTubeChannelCreate,
    YouTubeChannelRead,
)
from app.schemas.youtube_channels import YouTubeChannel
from app.services.staff_authz import require_dataset_permission
from app.services.video_ingestion_service import add_video_by_url, run_ingestion_cycle
from app.services.video_service import get_film_search_suggestions, get_video_feed
from app.utils.db_async import get_session

router = APIRouter(prefix="/api/videos", tags=["videos"])


@router.get("", response_model=VideoFeedResponse)
async def list_videos(
    limit: int = Query(default=20, ge=1, le=100, description="Items per page"),
    offset: int = Query(default=0, ge=0, description="Number of items to skip"),
    tag: Optional[str] = Query(default=None),
    channel_id: Optional[int] = Query(default=None),
    player_id: Optional[int] = Query(default=None),
    search: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(get_session),
) -> VideoFeedResponse:
    """Fetch film-room videos with optional filters."""
    return await get_video_feed(
        db=db,
        limit=limit,
        offset=offset,
        tag=tag,
        channel_id=channel_id,
        player_id=player_id,
        search=search,
    )


@router.get("/search-suggestions", response_model=FilmSearchSuggestionsResponse)
async def search_suggestions(
    q: str = Query(min_length=2, description="Search query"),
    db: AsyncSession = Depends(get_session),
) -> FilmSearchSuggestionsResponse:
    """Return typeahead suggestions for the film-room search bar."""
    return await get_film_search_suggestions(db, q)


@router.get(
    "/sources",
    response_model=list[YouTubeChannelRead],
    dependencies=[Depends(require_dataset_permission("youtube_channels", "view"))],
)
async def list_channels(
    db: AsyncSession = Depends(get_session),
) -> list[YouTubeChannelRead]:
    """List all YouTube channels (admin view)."""
    stmt = select(YouTubeChannel).order_by(YouTubeChannel.name)  # type: ignore[arg-type]
    channels = (await db.execute(stmt)).scalars().all()
    return [
        YouTubeChannelRead(
            id=channel.id or 0,
            name=channel.name,
            display_name=channel.display_name,
            channel_id=channel.channel_id,
            channel_url=channel.channel_url,
            thumbnail_url=channel.thumbnail_url,
            description=channel.description,
            is_draft_focused=channel.is_draft_focused,
            is_active=channel.is_active,
            fetch_interval_minutes=channel.fetch_interval_minutes,
            last_fetched_at=(
                channel.last_fetched_at.isoformat() if channel.last_fetched_at else None
            ),
        )
        for channel in channels
    ]


@router.post(
    "/sources",
    response_model=YouTubeChannelRead,
    status_code=201,
    dependencies=[Depends(require_dataset_permission("youtube_channels", "edit"))],
)
async def create_channel(
    payload: YouTubeChannelCreate,
    db: AsyncSession = Depends(get_session),
) -> YouTubeChannelRead:
    """Create a YouTube source channel (admin)."""
    async with db.begin():
        existing = await db.execute(
            select(YouTubeChannel).where(
                YouTubeChannel.channel_id == payload.channel_id  # type: ignore[arg-type]
            )
        )
        if existing.scalar_one_or_none() is not None:
            raise HTTPException(
                status_code=409, detail="Channel with this channel_id already exists"
            )

        now = datetime.now(UTC).replace(tzinfo=None)
        channel = YouTubeChannel(
            name=payload.name,
            display_name=payload.display_name,
            channel_id=payload.channel_id,
            channel_url=payload.channel_url,
            thumbnail_url=payload.thumbnail_url,
            description=payload.description,
            is_draft_focused=payload.is_draft_focused,
            is_active=True,
            fetch_interval_minutes=payload.fetch_interval_minutes,
            created_at=now,
            updated_at=now,
        )
        db.add(channel)
        await db.flush()

    return YouTubeChannelRead(
        id=channel.id or 0,
        name=channel.name,
        display_name=channel.display_name,
        channel_id=channel.channel_id,
        channel_url=channel.channel_url,
        thumbnail_url=channel.thumbnail_url,
        description=channel.description,
        is_draft_focused=channel.is_draft_focused,
        is_active=channel.is_active,
        fetch_interval_minutes=channel.fetch_interval_minutes,
        last_fetched_at=None,
    )


@router.post(
    "/ingest",
    response_model=VideoIngestionResult,
    dependencies=[Depends(require_dataset_permission("youtube_ingestion", "edit"))],
)
async def trigger_video_ingestion(
    db: AsyncSession = Depends(get_session),
) -> VideoIngestionResult:
    """Trigger YouTube ingestion cycle (admin)."""
    return await run_ingestion_cycle(db)


@router.post(
    "/add",
    response_model=dict[str, int],
    dependencies=[Depends(require_dataset_permission("youtube_videos", "edit"))],
)
async def manual_add_video(
    payload: ManualVideoAddRequest,
    db: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    """Add a film-room video by URL and optional manual player tags."""
    try:
        video_id = await add_video_by_url(
            db=db,
            youtube_url=payload.youtube_url,
            tag=payload.tag,
            player_ids=payload.player_ids,
        )
    except ValueError as exc:
        detail = str(exc)
        status_code = 503 if detail == "YOUTUBE_API_KEY is not configured" else 400
        raise HTTPException(status_code=status_code, detail=detail) from exc
    return {"id": video_id}
