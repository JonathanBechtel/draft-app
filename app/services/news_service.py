"""News feed retrieval service.

Handles fetching and formatting news items for display.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import Float, cast, func, literal, select, union
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.news import NewsFeedResponse, NewsItemRead
from app.schemas.player_content_mentions import ContentType, PlayerContentMention
from app.schemas.news_items import NewsItem, NewsItemTag
from app.schemas.news_sources import NewsSource
from app.schemas.players_master import PlayerMaster

# Shared column list for all news feed queries (general, hero, player-specific).
# Keep in sync with _row_to_news_item_read() which maps these columns.
_NEWS_FEED_COLUMNS = [
    NewsItem.id,
    NewsItem.title,
    NewsItem.summary,
    NewsItem.url,
    NewsItem.image_url,
    NewsItem.author,
    NewsItem.tag,
    NewsItem.published_at,
    NewsSource.display_name.label("source_name"),  # type: ignore[attr-defined]
]


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
) -> NewsFeedResponse:
    """Fetch paginated news feed with joined source info.

    Args:
        db: Async database session
        limit: Maximum items to return
        offset: Number of items to skip

    Returns:
        NewsFeedResponse with items and pagination info
    """
    # Build base query with JOIN
    base_query = (
        select(*_NEWS_FEED_COLUMNS)  # type: ignore[call-overload]
        .select_from(NewsItem)
        .join(NewsSource, NewsSource.id == NewsItem.source_id)  # type: ignore[arg-type]
    )

    # Count total items
    count_query = select(func.count()).select_from(NewsItem)
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
    items: list[NewsItemRead] = [_row_to_news_item_read(row) for row in rows]  # type: ignore[arg-type]

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


async def get_source_counts(db: AsyncSession) -> list[tuple[int, str, int]]:
    """Fetch active sources with article counts, sorted by count descending.

    Args:
        db: Async database session

    Returns:
        List of (source_id, display_name, article_count) tuples
    """
    stmt = (
        select(  # type: ignore[call-overload]
            NewsSource.id,
            NewsSource.display_name,
            func.count(NewsItem.id).label("cnt"),  # type: ignore[arg-type]
        )
        .select_from(NewsSource)
        .join(NewsItem, NewsItem.source_id == NewsSource.id)  # type: ignore[arg-type]
        .where(NewsSource.is_active.is_(True))  # type: ignore[attr-defined]
        .group_by(NewsSource.id, NewsSource.display_name)
        .order_by(func.count(NewsItem.id).desc(), NewsSource.display_name)  # type: ignore[arg-type,call-overload]
    )
    result = await db.execute(stmt)
    return [(row[0], row[1], row[2]) for row in result.all()]


async def get_hero_article(db: AsyncSession) -> Optional[NewsItemRead]:
    """Get the most recent article with an image for hero display.

    Args:
        db: Async database session

    Returns:
        Most recent NewsItemRead with image_url, or None if no articles have images
    """
    stmt = (
        select(*_NEWS_FEED_COLUMNS)  # type: ignore[call-overload]
        .select_from(NewsItem)
        .join(NewsSource, NewsSource.id == NewsItem.source_id)  # type: ignore[arg-type]
        .where(NewsItem.image_url.isnot(None))  # type: ignore[union-attr]
        .where(NewsItem.image_url != "")  # type: ignore[arg-type]
        .order_by(NewsItem.published_at.desc())  # type: ignore[attr-defined]
        .limit(1)
    )

    result = await db.execute(stmt)
    row = result.mappings().first()

    if not row:
        return None

    return _row_to_news_item_read(row)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class TrendingPlayer:
    """A player trending in the news based on recency-weighted mention volume."""

    player_id: int
    display_name: str
    slug: str
    school: Optional[str]
    mention_count: int
    trending_score: float
    daily_counts: list[int] = field(default_factory=list)
    latest_mention_at: Optional[datetime] = None


async def get_trending_players(
    db: AsyncSession,
    days: int = 7,
    limit: int = 10,
    content_type: ContentType | None = None,
) -> list[TrendingPlayer]:
    """Get players with the most content mentions, ranked by recency-weighted score.

    Uses linear decay: a mention from today has weight 1.0, a mention from
    ``days`` ago has weight ~0.0.  The trending score is SUM(weights).
    Raw ``mention_count`` is still returned for badge display.

    Aggregates across ALL content types (news + podcasts) by default, or
    a single content type when ``content_type`` is provided.

    Args:
        db: Async database session
        days: Number of days to look back for mentions
        limit: Maximum number of players to return
        content_type: Optional filter to scope to a single content type

    Returns:
        List of TrendingPlayer sorted by trending_score desc
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = now - timedelta(days=days)

    # Age in fractional days (using denormalized published_at on the mention row)
    age_seconds = func.extract(
        "epoch",
        literal(now) - PlayerContentMention.published_at,
    )
    age_days = age_seconds / 86400.0
    weight = func.greatest(1.0 - age_days / days, 0.0)

    stmt = (
        select(
            PlayerContentMention.player_id,
            PlayerMaster.display_name,
            PlayerMaster.slug,
            PlayerMaster.school,
            func.count().label("mention_count"),  # type: ignore[call-overload]
            cast(func.sum(weight), Float).label("trending_score"),
            func.max(PlayerContentMention.published_at).label("latest_mention_at"),  # type: ignore[call-overload]
        )
        .join(
            PlayerMaster,
            PlayerMaster.id == PlayerContentMention.player_id,  # type: ignore[arg-type]
        )
        .where(PlayerContentMention.published_at >= cutoff)  # type: ignore[arg-type,operator]
        .group_by(
            PlayerContentMention.player_id,
            PlayerMaster.display_name,
            PlayerMaster.slug,
            PlayerMaster.school,
        )
        .order_by(
            cast(func.sum(weight), Float).desc(),
            func.count().desc(),  # type: ignore[call-overload]
        )
        .limit(limit)
    )

    if content_type is not None:
        stmt = stmt.where(
            PlayerContentMention.content_type == content_type.value  # type: ignore[arg-type]
        )

    result = await db.execute(stmt)
    rows = result.mappings().all()

    if not rows:
        return []

    player_ids = [row["player_id"] for row in rows]
    daily_map = await _get_daily_mention_counts(db, player_ids, days, content_type)

    return [
        TrendingPlayer(
            player_id=row["player_id"],
            display_name=row["display_name"] or "Unknown",
            slug=row["slug"] or "",
            school=row["school"],
            mention_count=row["mention_count"],
            trending_score=round(float(row["trending_score"] or 0), 3),
            daily_counts=daily_map.get(row["player_id"], [0] * days),
            latest_mention_at=row["latest_mention_at"],
        )
        for row in rows
    ]


async def _get_daily_mention_counts(
    db: AsyncSession,
    player_ids: list[int],
    days: int = 7,
    content_type: ContentType | None = None,
) -> dict[int, list[int]]:
    """Fetch per-day mention counts for a set of players.

    Buckets by ``date_trunc('day', player_content_mentions.published_at)``
    and fills zeros for days with no mentions.  Aggregates across all
    content types by default, or a single type when ``content_type`` is given.

    Args:
        db: Async database session
        player_ids: Player IDs to fetch counts for
        days: Number of days to look back
        content_type: Optional filter to scope to a single content type

    Returns:
        Mapping of player_id → list of daily counts (oldest-first, length=days)
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = now - timedelta(days=days)

    day_col = func.date_trunc("day", PlayerContentMention.published_at).label(
        "mention_day"
    )

    stmt = (
        select(
            PlayerContentMention.player_id,
            day_col,
            func.count().label("cnt"),  # type: ignore[call-overload]
        )
        .where(PlayerContentMention.published_at >= cutoff)  # type: ignore[arg-type,operator]
        .where(
            PlayerContentMention.player_id.in_(player_ids)  # type: ignore[attr-defined]
        )
        .group_by(PlayerContentMention.player_id, day_col)
    )

    if content_type is not None:
        stmt = stmt.where(
            PlayerContentMention.content_type == content_type.value  # type: ignore[arg-type]
        )

    result = await db.execute(stmt)
    rows = result.mappings().all()

    # Build date labels for the window (oldest first)
    date_labels = [(now - timedelta(days=days - 1 - i)).date() for i in range(days)]

    # Organise raw rows into pid → {date: count}
    raw: dict[int, dict] = {}
    for row in rows:
        pid = row["player_id"]
        day = row["mention_day"]
        # date_trunc returns a datetime; normalise to date
        if isinstance(day, datetime):
            day = day.date()
        raw.setdefault(pid, {})[day] = row["cnt"]

    # Fill zeros for missing days
    result_map: dict[int, list[int]] = {}
    for pid in player_ids:
        counts = raw.get(pid, {})
        result_map[pid] = [counts.get(d, 0) for d in date_labels]

    return result_map


async def get_filtered_news_feed(
    db: AsyncSession,
    limit: int = 20,
    offset: int = 0,
    tag: str | None = None,
    source_id: int | None = None,
    author: str | None = None,
    player_id: int | None = None,
    period: str | None = None,
) -> NewsFeedResponse:
    """Fetch paginated news feed with optional multi-dimensional filters.

    All non-None filters are combined with AND logic.

    Args:
        db: Async database session
        limit: Maximum items to return
        offset: Number of items to skip
        tag: Filter by NewsItemTag value (e.g. "Mock Draft")
        source_id: Filter by news source ID
        author: Filter by author name (case-insensitive)
        player_id: Filter to articles mentioning this player
        period: Time window filter ("today", "week", "month", or None for all)

    Returns:
        NewsFeedResponse with filtered items and accurate total
    """
    base_query = (
        select(*_NEWS_FEED_COLUMNS)  # type: ignore[call-overload]
        .select_from(NewsItem)
        .join(NewsSource, NewsSource.id == NewsItem.source_id)  # type: ignore[arg-type]
    )

    count_base = select(func.count()).select_from(NewsItem)

    # Apply filters to both queries
    if tag:
        # Resolve display value ("Scouting Report") to DB name ("SCOUTING_REPORT")
        try:
            tag_db = NewsItemTag(tag).name
        except ValueError:
            tag_db = None
        if tag_db:
            base_query = base_query.where(NewsItem.tag == tag_db)  # type: ignore[arg-type]
            count_base = count_base.where(NewsItem.tag == tag_db)  # type: ignore[arg-type]

    if source_id is not None:
        base_query = base_query.where(NewsItem.source_id == source_id)  # type: ignore[arg-type]
        count_base = count_base.where(NewsItem.source_id == source_id)  # type: ignore[arg-type]

    if author:
        base_query = base_query.where(
            func.lower(NewsItem.author) == author.lower()  # type: ignore[union-attr]
        )
        count_base = count_base.where(
            func.lower(NewsItem.author) == author.lower()  # type: ignore[union-attr]
        )

    if player_id is not None:
        mention_subq = (
            select(  # type: ignore[call-overload]
                PlayerContentMention.content_id.label("item_id")  # type: ignore[attr-defined]
            )
            .where(PlayerContentMention.player_id == player_id)  # type: ignore[arg-type]
            .where(PlayerContentMention.content_type == ContentType.NEWS.value)  # type: ignore[arg-type]
        )
        base_query = base_query.where(
            NewsItem.id.in_(mention_subq)  # type: ignore[union-attr]
        )
        count_base = count_base.where(
            NewsItem.id.in_(mention_subq)  # type: ignore[union-attr]
        )

    if period:
        now = datetime.utcnow()
        cutoff_map = {"today": 1, "week": 7, "month": 30}
        days = cutoff_map.get(period, 0)
        if days:
            cutoff = now - timedelta(days=days)
            base_query = base_query.where(
                NewsItem.published_at >= cutoff  # type: ignore[arg-type,operator]
            )
            count_base = count_base.where(
                NewsItem.published_at >= cutoff  # type: ignore[arg-type,operator]
            )

    # Execute count
    count_result = await db.execute(count_base)
    total = count_result.scalar() or 0

    # Execute paginated items
    items_query = (
        base_query.order_by(NewsItem.published_at.desc())  # type: ignore[attr-defined]
        .limit(limit)
        .offset(offset)
    )
    result = await db.execute(items_query)
    rows = result.mappings().all()

    items: list[NewsItemRead] = [_row_to_news_item_read(row) for row in rows]  # type: ignore[arg-type]

    return NewsFeedResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )


async def get_author_counts(db: AsyncSession) -> list[tuple[str, int]]:
    """Fetch author names with article counts, sorted by count descending.

    Args:
        db: Async database session

    Returns:
        List of (author_name, article_count) tuples, highest count first
    """
    stmt = (
        select(  # type: ignore[call-overload]
            NewsItem.author,
            func.count().label("cnt"),
        )
        .where(NewsItem.author.isnot(None))  # type: ignore[union-attr]
        .where(NewsItem.author != "")  # type: ignore[arg-type]
        .group_by(NewsItem.author)
        .order_by(func.count().desc(), NewsItem.author)  # type: ignore[call-overload]
    )
    result = await db.execute(stmt)
    return [(row[0], row[1]) for row in result.all() if row[0]]


async def get_player_news_feed(
    db: AsyncSession,
    player_id: int,
    limit: int = 20,
    offset: int = 0,
    min_items: int = 5,
) -> NewsFeedResponse:
    """Fetch news feed for a specific player.

    Queries articles where the player has a mention row OR where
    NewsItem.player_id matches. If results are below min_items,
    backfills with general feed items (excluding already-included IDs).

    Args:
        db: Async database session
        player_id: Player to get news for
        limit: Maximum items to return
        offset: Number of items to skip
        min_items: Minimum items before backfilling with general feed

    Returns:
        NewsFeedResponse with items marked with is_player_specific
    """
    # Subquery: news_item IDs that mention this player (via junction table)
    mention_subq = (
        select(  # type: ignore[call-overload]
            PlayerContentMention.content_id.label("item_id")  # type: ignore[attr-defined]
        )
        .where(PlayerContentMention.player_id == player_id)  # type: ignore[arg-type]
        .where(PlayerContentMention.content_type == ContentType.NEWS.value)  # type: ignore[arg-type]
    )

    # Subquery: news_item IDs where player_id is set directly
    direct_subq = select(  # type: ignore[call-overload]
        NewsItem.id.label("item_id")  # type: ignore[union-attr]
    ).where(
        NewsItem.player_id == player_id  # type: ignore[arg-type]
    )

    # Union of both sources (union deduplicates IDs that appear in both)
    combined = union(mention_subq, direct_subq).subquery()

    # Player-specific query
    player_query = (
        select(*_NEWS_FEED_COLUMNS)  # type: ignore[call-overload]
        .select_from(NewsItem)
        .join(NewsSource, NewsSource.id == NewsItem.source_id)  # type: ignore[arg-type]
        .where(NewsItem.id.in_(select(combined.c.item_id)))  # type: ignore[union-attr]
        .order_by(NewsItem.published_at.desc())  # type: ignore[attr-defined]
        .limit(limit)
        .offset(offset)
    )

    result = await _execute_mappings(db, player_query)
    player_item_ids: set[int] = set()
    items: list[NewsItemRead] = []

    for row in result:
        player_item_ids.add(row["id"])
        items.append(_row_to_news_item_read(row, is_player_specific=True))

    # Backfill with general feed if insufficient player-specific articles
    if len(items) < min_items and offset == 0:
        backfill_needed = min_items - len(items)
        backfill_query = (
            select(*_NEWS_FEED_COLUMNS)  # type: ignore[call-overload]
            .select_from(NewsItem)
            .join(NewsSource, NewsSource.id == NewsItem.source_id)  # type: ignore[arg-type]
            .order_by(NewsItem.published_at.desc())  # type: ignore[attr-defined]
            .limit(backfill_needed)
        )

        if player_item_ids:
            backfill_query = backfill_query.where(
                NewsItem.id.notin_(list(player_item_ids))  # type: ignore[union-attr]
            )

        backfill_result = await _execute_mappings(db, backfill_query)
        for row in backfill_result:
            if len(items) >= limit:
                break
            items.append(_row_to_news_item_read(row, is_player_specific=False))

    # Count total player-specific items
    count_query = (
        select(func.count())
        .select_from(NewsItem)
        .where(NewsItem.id.in_(select(combined.c.item_id)))  # type: ignore[union-attr]
    )
    count_result = await db.execute(count_query)
    total = count_result.scalar() or 0

    return NewsFeedResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )


async def _execute_mappings(db: AsyncSession, query: object) -> list[dict]:
    """Execute a SELECT query and return rows as mapping dicts."""
    result = await db.execute(query)  # type: ignore[arg-type,call-overload]
    return list(result.mappings().all())  # type: ignore[union-attr]


def _resolve_tag(raw: str | NewsItemTag) -> str:
    """Return the display value for a tag stored as either name or value."""
    if isinstance(raw, NewsItemTag):
        return raw.value
    try:
        return NewsItemTag(raw).value
    except ValueError:
        return NewsItemTag[raw].value


def _row_to_news_item_read(row: dict, is_player_specific: bool = False) -> NewsItemRead:
    """Convert a database row mapping to a NewsItemRead response model."""
    source_name = row["source_name"]
    return NewsItemRead(
        id=row["id"],
        source_name=source_name,
        title=row["title"],
        summary=row["summary"] or "",
        url=row["url"],
        image_url=row["image_url"],
        author=row["author"],
        time=format_relative_time(row["published_at"]),
        tag=_resolve_tag(row["tag"]),
        read_more_text=build_read_more_text(source_name),
        is_player_specific=is_player_specific,
    )
