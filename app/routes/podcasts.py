"""Podcast feed API routes.

Provides endpoints for:
- Fetching paginated podcast feed
- Managing podcast shows (admin)
- Triggering podcast ingestion (admin)
"""

from datetime import UTC, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.podcasts import (
    PodcastFeedResponse,
    PodcastIngestionResult,
    PodcastShowCreate,
    PodcastShowRead,
)
from app.schemas.podcast_shows import PodcastShow
from app.services.podcast_ingestion_service import run_ingestion_cycle
from app.services.podcast_service import (
    get_active_shows,
    get_podcast_feed,
    get_player_podcast_feed,
)
from app.services.staff_authz import require_dataset_permission
from app.utils.db_async import SessionLocal, dispose_engine, get_session

router = APIRouter(prefix="/api/podcasts", tags=["podcasts"])


@router.get("", response_model=PodcastFeedResponse)
async def list_podcasts(
    limit: int = Query(default=20, ge=1, le=100, description="Items per page"),
    offset: int = Query(default=0, ge=0, description="Number of items to skip"),
    player_id: Optional[int] = Query(default=None, description="Filter by player ID"),
    db: AsyncSession = Depends(get_session),
) -> PodcastFeedResponse:
    """Fetch paginated podcast feed.

    Returns podcast episodes with AI-generated summaries, sorted by published date.
    When player_id is provided, returns player-specific feed (mentions + direct
    association).
    """
    if player_id is not None:
        return await get_player_podcast_feed(
            db=db,
            player_id=player_id,
            limit=limit,
            offset=offset,
        )
    return await get_podcast_feed(
        db=db,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/sources",
    response_model=list[PodcastShowRead],
    dependencies=[Depends(require_dataset_permission("podcasts", "view"))],
)
async def list_shows(
    db: AsyncSession = Depends(get_session),
) -> list[PodcastShowRead]:
    """List all active podcast shows (admin view)."""
    shows = await get_active_shows(db)

    return [
        PodcastShowRead(
            id=show.id or 0,
            name=show.name,
            display_name=show.display_name,
            feed_url=show.feed_url,
            artwork_url=show.artwork_url,
            author=show.author,
            description=show.description,
            website_url=show.website_url,
            is_draft_focused=show.is_draft_focused,
            is_active=show.is_active,
            fetch_interval_minutes=show.fetch_interval_minutes,
            last_fetched_at=(
                show.last_fetched_at.isoformat() if show.last_fetched_at else None
            ),
        )
        for show in shows
    ]


@router.post(
    "/sources",
    response_model=PodcastShowRead,
    status_code=201,
    dependencies=[Depends(require_dataset_permission("podcasts", "edit"))],
)
async def create_show(
    show_data: PodcastShowCreate,
    db: AsyncSession = Depends(get_session),
) -> PodcastShowRead:
    """Add a new podcast show (admin).

    Creates a new podcast show that will be ingested on the next cycle.
    """
    async with db.begin():
        # Check for duplicate feed URL
        existing_stmt = select(PodcastShow).where(
            PodcastShow.feed_url == show_data.feed_url  # type: ignore[arg-type]
        )
        existing = await db.execute(existing_stmt)
        if existing.scalar_one_or_none() is not None:
            raise HTTPException(
                status_code=409, detail="Show with this feed URL already exists"
            )

        now = datetime.now(UTC).replace(tzinfo=None)
        show = PodcastShow(
            name=show_data.name,
            display_name=show_data.display_name,
            feed_url=show_data.feed_url,
            artwork_url=show_data.artwork_url,
            author=show_data.author,
            description=show_data.description,
            website_url=show_data.website_url,
            is_draft_focused=show_data.is_draft_focused,
            is_active=True,
            fetch_interval_minutes=show_data.fetch_interval_minutes,
            created_at=now,
            updated_at=now,
        )
        db.add(show)
        await db.flush()

    return PodcastShowRead(
        id=show.id or 0,
        name=show.name,
        display_name=show.display_name,
        feed_url=show.feed_url,
        artwork_url=show.artwork_url,
        author=show.author,
        description=show.description,
        website_url=show.website_url,
        is_draft_focused=show.is_draft_focused,
        is_active=show.is_active,
        fetch_interval_minutes=show.fetch_interval_minutes,
        last_fetched_at=None,
    )


@router.post(
    "/ingest",
    response_model=PodcastIngestionResult,
    dependencies=[Depends(require_dataset_permission("podcast_ingestion", "edit"))],
)
async def trigger_podcast_ingestion(
    db: AsyncSession = Depends(get_session),
) -> PodcastIngestionResult:
    """Trigger podcast ingestion cycle (admin).

    Fetches all active shows, parses RSS feeds, generates AI summaries,
    and stores new episodes.
    """
    result = await run_ingestion_cycle(db)

    cache_error_markers = (
        "cache lookup failed for type",
        "InvalidCachedStatementError",
        "cached statement plan is invalid",
    )
    if any(
        any(marker in err for marker in cache_error_markers) for err in result.errors
    ):
        await dispose_engine()
        async with SessionLocal() as retry_db:
            return await run_ingestion_cycle(retry_db)

    return result
