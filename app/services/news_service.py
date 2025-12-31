"""News feed retrieval service.

Handles fetching and formatting news items for display.
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.news import NewsFeedResponse, NewsItemRead
from app.schemas.news_items import NewsItem
from app.schemas.news_sources import NewsSource


def format_relative_time(dt: datetime) -> str:
    """Convert datetime to relative time string.

    Args:
        dt: Datetime to format (assumed UTC)

    Returns:
        Relative time like '2h', '1d', '3d', '1w'
    """
    now = datetime.now(timezone.utc)

    # Ensure dt is timezone-aware
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    delta = now - dt
    total_seconds = delta.total_seconds()

    if total_seconds < 0:
        return "now"

    minutes = int(total_seconds / 60)
    hours = int(total_seconds / 3600)
    days = int(total_seconds / 86400)
    weeks = int(days / 7)

    if minutes < 60:
        return f"{max(1, minutes)}m"
    elif hours < 24:
        return f"{hours}h"
    elif days < 7:
        return f"{days}d"
    else:
        return f"{weeks}w"


def build_read_more_text(source_name: str) -> str:
    """Generate 'Read at [Source]' CTA text.

    Args:
        source_name: Display name of the source

    Returns:
        CTA text like 'Read at Floor and Ceiling'
    """
    return f"Read at {source_name}"


async def get_news_feed(
    db: AsyncSession,
    limit: int = 20,
    offset: int = 0,
    player_id: Optional[int] = None,
) -> NewsFeedResponse:
    """Fetch paginated news feed with joined source info.

    Args:
        db: Async database session
        limit: Maximum items to return
        offset: Number of items to skip
        player_id: Optional filter by player ID

    Returns:
        NewsFeedResponse with items and pagination info
    """
    # Build base query with JOIN
    base_query = (
        select(
            NewsItem.id,
            NewsItem.title,
            NewsItem.summary,
            NewsItem.url,
            NewsItem.image_url,
            NewsItem.author,
            NewsItem.tag,
            NewsItem.published_at,
            NewsSource.display_name.label("source_name"),  # type: ignore[attr-defined]
        )  # type: ignore[call-overload]
        .select_from(NewsItem)
        .join(NewsSource, NewsSource.id == NewsItem.source_id)
    )

    # Apply player filter if provided
    if player_id is not None:
        base_query = base_query.where(
            NewsItem.player_id == player_id  # type: ignore[arg-type]
        )

    # Count total items
    count_query = select(func.count()).select_from(NewsItem)
    if player_id is not None:
        count_query = count_query.where(
            NewsItem.player_id == player_id  # type: ignore[arg-type]
        )
    count_result = await db.execute(count_query)
    total = count_result.scalar() or 0

    # Fetch paginated items
    items_query = (
        base_query.order_by(NewsItem.published_at.desc())  # type: ignore[attr-defined]
        .limit(limit)
        .offset(offset)
    )
    result = await db.execute(items_query)
    rows = result.mappings().all()

    # Transform to response models
    items: list[NewsItemRead] = []
    for row in rows:
        source_name = row["source_name"]
        summary = row["summary"] or ""

        items.append(
            NewsItemRead(
                id=row["id"],
                source_name=source_name,
                title=row["title"],
                summary=summary,
                url=row["url"],
                image_url=row["image_url"],
                author=row["author"],
                time=format_relative_time(row["published_at"]),
                tag=row["tag"].value,
                read_more_text=build_read_more_text(source_name),
            )
        )

    return NewsFeedResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )


async def get_active_sources(db: AsyncSession) -> list[NewsSource]:
    """Fetch all active news sources.

    Args:
        db: Async database session

    Returns:
        List of active NewsSource records
    """
    stmt = select(NewsSource).where(
        NewsSource.is_active.is_(True)  # type: ignore[attr-defined]
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())
