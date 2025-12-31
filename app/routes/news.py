"""News feed API routes.

Provides endpoints for:
- Fetching paginated news feed
- Managing news sources (admin)
- Triggering feed ingestion (admin)
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.news import (
    IngestionResult,
    NewsFeedResponse,
    NewsSourceCreate,
    NewsSourceRead,
)
from app.schemas.news_sources import FeedType, NewsSource
from app.services.news_ingestion_service import run_ingestion_cycle
from app.services.news_service import get_news_feed
from app.utils.db_async import get_session

router = APIRouter(prefix="/api/news", tags=["news"])


@router.get("", response_model=NewsFeedResponse)
async def list_news(
    limit: int = Query(default=20, ge=1, le=100, description="Items per page"),
    offset: int = Query(default=0, ge=0, description="Number of items to skip"),
    player_id: Optional[int] = Query(default=None, description="Filter by player ID"),
    db: AsyncSession = Depends(get_session),
) -> NewsFeedResponse:
    """Fetch paginated news feed.

    Returns news items with AI-generated summaries, sorted by published date.
    Optionally filter by player ID for player-specific news.
    """
    return await get_news_feed(
        db=db,
        limit=limit,
        offset=offset,
        player_id=player_id,
    )


@router.get("/sources", response_model=list[NewsSourceRead])
async def list_sources(
    db: AsyncSession = Depends(get_session),
) -> list[NewsSourceRead]:
    """List all news sources (admin view).

    Returns source configuration including feed URLs and fetch status.
    """
    stmt = select(NewsSource).order_by(NewsSource.name)
    result = await db.execute(stmt)
    sources = result.scalars().all()

    return [
        NewsSourceRead(
            id=source.id or 0,
            name=source.name,
            display_name=source.display_name,
            feed_type=source.feed_type.value,
            feed_url=source.feed_url,
            is_active=source.is_active,
            fetch_interval_minutes=source.fetch_interval_minutes,
            last_fetched_at=(
                source.last_fetched_at.isoformat() if source.last_fetched_at else None
            ),
        )
        for source in sources
    ]


@router.post("/sources", response_model=NewsSourceRead, status_code=201)
async def create_source(
    source_data: NewsSourceCreate,
    db: AsyncSession = Depends(get_session),
) -> NewsSourceRead:
    """Add a new news source (admin).

    Creates a new RSS source that will be ingested on the next cycle.
    """
    # Check for duplicate feed URL
    existing_stmt = select(NewsSource).where(
        NewsSource.feed_url == source_data.feed_url  # type: ignore[arg-type]
    )
    existing = await db.execute(existing_stmt)
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=409, detail="Source with this feed URL already exists"
        )

    # Validate feed type
    try:
        feed_type = FeedType(source_data.feed_type)
    except ValueError:
        raise HTTPException(
            status_code=400, detail=f"Invalid feed type: {source_data.feed_type}"
        )

    # Create new source
    source = NewsSource(
        name=source_data.name,
        display_name=source_data.display_name,
        feed_type=feed_type,
        feed_url=source_data.feed_url,
        fetch_interval_minutes=source_data.fetch_interval_minutes,
        is_active=True,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(source)
    await db.commit()
    await db.refresh(source)

    return NewsSourceRead(
        id=source.id or 0,
        name=source.name,
        display_name=source.display_name,
        feed_type=source.feed_type.value,
        feed_url=source.feed_url,
        is_active=source.is_active,
        fetch_interval_minutes=source.fetch_interval_minutes,
        last_fetched_at=None,
    )


@router.post("/ingest", response_model=IngestionResult)
async def trigger_ingestion(
    db: AsyncSession = Depends(get_session),
) -> IngestionResult:
    """Trigger feed ingestion cycle (admin).

    Fetches all active sources, parses feeds, generates AI summaries,
    and stores new items. This endpoint can be called manually for
    testing or by an external cron job in production.
    """
    result = await run_ingestion_cycle(db)
    return result
