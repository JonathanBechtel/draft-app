"""News feed API routes.

Provides endpoints for:
- Fetching paginated news feed
- Managing news sources (admin)
- Triggering feed ingestion (admin)
"""

from datetime import UTC, datetime
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
from app.services.news_service import get_news_feed, get_player_news_feed
from app.services.staff_authz import require_dataset_permission
from app.utils.db_async import SessionLocal, dispose_engine, get_session

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
    When player_id is provided, returns player-specific feed (mentions + direct
    association) with general feed backfill.
    """
    if player_id is not None:
        return await get_player_news_feed(
            db=db,
            player_id=player_id,
            limit=limit,
            offset=offset,
        )
    return await get_news_feed(
        db=db,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/sources",
    response_model=list[NewsSourceRead],
    dependencies=[Depends(require_dataset_permission("news_sources", "view"))],
)
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


@router.post(
    "/sources",
    response_model=NewsSourceRead,
    status_code=201,
    dependencies=[Depends(require_dataset_permission("news_sources", "edit"))],
)
async def create_source(
    source_data: NewsSourceCreate,
    db: AsyncSession = Depends(get_session),
) -> NewsSourceRead:
    """Add a new news source (admin).

    Creates a new RSS source that will be ingested on the next cycle.
    """
    async with db.begin():
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
                status_code=400,
                detail=f"Invalid feed type: {source_data.feed_type}",
            )

        # Create new source
        source = NewsSource(
            name=source_data.name,
            display_name=source_data.display_name,
            feed_type=feed_type,
            feed_url=source_data.feed_url,
            fetch_interval_minutes=source_data.fetch_interval_minutes,
            is_active=True,
            created_at=datetime.now(UTC).replace(tzinfo=None),
            updated_at=datetime.now(UTC).replace(tzinfo=None),
        )
        db.add(source)
        await db.flush()

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


@router.post(
    "/ingest",
    response_model=IngestionResult,
    dependencies=[Depends(require_dataset_permission("news_ingestion", "edit"))],
)
async def trigger_ingestion(
    db: AsyncSession = Depends(get_session),
) -> IngestionResult:
    """Trigger feed ingestion cycle (admin).

    Fetches all active sources, parses feeds, generates AI summaries,
    and stores new items. This endpoint can be called manually for
    testing or by an external cron job in production.
    """
    result = await run_ingestion_cycle(db)

    # If the database schema changed (common in dev when running Alembic while the
    # app is live), asyncpg can error with stale type/statement caches (notably for
    # enum migrations). In that case, dispose the engine and retry once with a fresh
    # connection so a single manual POST succeeds.
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
