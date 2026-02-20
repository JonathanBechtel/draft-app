"""Podcast feed retrieval service.

Handles fetching and formatting podcast episodes for display.
"""

from typing import Any

from sqlalchemy import func, select, union
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.podcasts import PodcastEpisodeRead, PodcastFeedResponse
from app.schemas.player_content_mentions import ContentType, PlayerContentMention
from app.schemas.podcast_episodes import PodcastEpisode
from app.schemas.podcast_shows import PodcastShow
from app.services.news_service import format_relative_time

# Shared column list for podcast feed queries.
# Keep in sync with _row_to_episode_read() which maps these columns.
_PODCAST_FEED_COLUMNS = [
    PodcastEpisode.id,
    PodcastEpisode.title,
    PodcastEpisode.summary,
    PodcastEpisode.audio_url,
    PodcastEpisode.episode_url,
    PodcastEpisode.artwork_url,
    PodcastEpisode.duration_seconds,
    PodcastEpisode.tag,
    PodcastEpisode.published_at,
    PodcastShow.display_name.label("show_name"),  # type: ignore[attr-defined]
]


def format_duration(seconds: int | None) -> str:
    """Convert duration in seconds to a human-readable string.

    Args:
        seconds: Duration in seconds, or None if unknown

    Returns:
        Formatted string like "45:23" or "1:02:03", or "" if unknown
    """
    if seconds is None or seconds < 0:
        return ""
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def build_listen_on_text(show_name: str) -> str:
    """Generate 'Listen on [Show]' CTA text.

    Args:
        show_name: Display name of the podcast show

    Returns:
        CTA text like 'Listen on The Ringer NBA Draft Show'
    """
    return f"Listen on {show_name}"


async def get_podcast_feed(
    db: AsyncSession,
    limit: int = 20,
    offset: int = 0,
) -> PodcastFeedResponse:
    """Fetch paginated podcast feed with joined show info.

    Args:
        db: Async database session
        limit: Maximum items to return
        offset: Number of items to skip

    Returns:
        PodcastFeedResponse with items and pagination info
    """
    base_query = (
        select(*_PODCAST_FEED_COLUMNS)  # type: ignore[call-overload]
        .select_from(PodcastEpisode)
        .join(PodcastShow, PodcastShow.id == PodcastEpisode.show_id)  # type: ignore[arg-type]
    )

    count_query = select(func.count()).select_from(PodcastEpisode)
    count_result = await db.execute(count_query)
    total = count_result.scalar() or 0

    items_query = (
        base_query.order_by(PodcastEpisode.published_at.desc())  # type: ignore[attr-defined]
        .limit(limit)
        .offset(offset)
    )
    result = await db.execute(items_query)
    rows = result.mappings().all()

    items = [_row_to_episode_read(row) for row in rows]  # type: ignore[arg-type]

    return PodcastFeedResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )


async def get_latest_podcast_episodes(
    db: AsyncSession,
    limit: int = 6,
) -> list[PodcastEpisodeRead]:
    """Fetch the most recent podcast episodes for homepage display.

    Args:
        db: Async database session
        limit: Maximum episodes to return

    Returns:
        List of PodcastEpisodeRead sorted by published_at desc
    """
    query = (
        select(*_PODCAST_FEED_COLUMNS)  # type: ignore[call-overload]
        .select_from(PodcastEpisode)
        .join(PodcastShow, PodcastShow.id == PodcastEpisode.show_id)  # type: ignore[arg-type]
        .order_by(PodcastEpisode.published_at.desc())  # type: ignore[attr-defined]
        .limit(limit)
    )
    result = await db.execute(query)
    rows = result.mappings().all()
    return [_row_to_episode_read(row) for row in rows]  # type: ignore[arg-type]


async def get_player_podcast_feed(
    db: AsyncSession,
    player_id: int,
    limit: int = 20,
    offset: int = 0,
) -> PodcastFeedResponse:
    """Fetch podcast feed for a specific player.

    Queries episodes where the player has a mention row (via
    PlayerContentMention with content_type=PODCAST) OR where
    PodcastEpisode.player_id matches directly.

    Args:
        db: Async database session
        player_id: Player to get episodes for
        limit: Maximum items to return
        offset: Number of items to skip

    Returns:
        PodcastFeedResponse with items marked as player-specific
    """
    # Subquery: episode IDs via mention junction table
    mention_subq = (
        select(  # type: ignore[call-overload]
            PlayerContentMention.content_id.label("item_id")  # type: ignore[attr-defined]
        )
        .where(PlayerContentMention.player_id == player_id)  # type: ignore[arg-type]
        .where(PlayerContentMention.content_type == ContentType.PODCAST)  # type: ignore[arg-type]
    )

    # Subquery: episode IDs via direct player_id column
    direct_subq = select(  # type: ignore[call-overload]
        PodcastEpisode.id.label("item_id")  # type: ignore[union-attr]
    ).where(
        PodcastEpisode.player_id == player_id  # type: ignore[arg-type]
    )

    combined = union(mention_subq, direct_subq).subquery()

    player_query = (
        select(*_PODCAST_FEED_COLUMNS)  # type: ignore[call-overload]
        .select_from(PodcastEpisode)
        .join(PodcastShow, PodcastShow.id == PodcastEpisode.show_id)  # type: ignore[arg-type]
        .where(PodcastEpisode.id.in_(select(combined.c.item_id)))  # type: ignore[union-attr]
        .order_by(PodcastEpisode.published_at.desc())  # type: ignore[attr-defined]
        .limit(limit)
        .offset(offset)
    )

    result = await db.execute(player_query)
    rows = result.mappings().all()
    items = [_row_to_episode_read(row, is_player_specific=True) for row in rows]  # type: ignore[arg-type]

    # Count total player-specific episodes
    count_query = (
        select(func.count())
        .select_from(PodcastEpisode)
        .where(PodcastEpisode.id.in_(select(combined.c.item_id)))  # type: ignore[union-attr]
    )
    count_result = await db.execute(count_query)
    total = count_result.scalar() or 0

    return PodcastFeedResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )


async def get_active_shows(db: AsyncSession) -> list[PodcastShow]:
    """Fetch all active podcast shows.

    Args:
        db: Async database session

    Returns:
        List of active PodcastShow records
    """
    stmt = select(PodcastShow).where(
        PodcastShow.is_active.is_(True)  # type: ignore[attr-defined]
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


def _row_to_episode_read(
    row: dict[str, Any], is_player_specific: bool = False
) -> PodcastEpisodeRead:
    """Convert a database row mapping to a PodcastEpisodeRead response model."""
    show_name = row["show_name"]
    return PodcastEpisodeRead(
        id=row["id"],
        show_name=show_name,
        artwork_url=row["artwork_url"],
        title=row["title"],
        summary=row["summary"] or "",
        tag=row["tag"].value,
        audio_url=row["audio_url"],
        episode_url=row["episode_url"],
        duration=format_duration(row["duration_seconds"]),
        time=format_relative_time(row["published_at"]),
        listen_on_text=build_listen_on_text(show_name),
        is_player_specific=is_player_specific,
    )
