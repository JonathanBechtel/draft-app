"""Podcast feed retrieval service.

Handles fetching and formatting podcast episodes for display.
"""

from typing import Any

from sqlalchemy import func, select, union
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.podcasts import MentionedPlayer, PodcastEpisodeRead, PodcastFeedResponse
from app.schemas.player_content_mentions import ContentType, PlayerContentMention
from app.schemas.players_master import PlayerMaster
from app.schemas.podcast_episodes import PodcastEpisode, PodcastEpisodeTag
from app.schemas.podcast_shows import PodcastShow
from app.services.news_service import (
    TrendingPlayer,
    format_relative_time,
    get_trending_players,
)

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
    PodcastShow.artwork_url.label("show_artwork_url"),  # type: ignore[union-attr]
]


def _coerce_podcast_tag(raw: str) -> PodcastEpisodeTag | None:
    """Parse a tag string that may be an enum value or enum name."""
    try:
        return PodcastEpisodeTag(raw)
    except ValueError:
        try:
            return PodcastEpisodeTag[raw]
        except KeyError:
            return None


def _resolve_podcast_tag(raw: str | PodcastEpisodeTag) -> str:
    """Return display text for a podcast tag stored as enum, name, or value."""
    if isinstance(raw, PodcastEpisodeTag):
        return raw.value
    try:
        return PodcastEpisodeTag(raw).value
    except ValueError:
        try:
            return PodcastEpisodeTag[raw].value
        except KeyError:
            return raw


async def _load_mentions_for_episodes(
    db: AsyncSession,
    episode_ids: list[int],
) -> dict[int, list[MentionedPlayer]]:
    """Batch-load player mentions for a set of podcast episodes.

    Args:
        db: Async database session
        episode_ids: List of episode (content) IDs to load mentions for

    Returns:
        Dict mapping episode ID to list of MentionedPlayer
    """
    if not episode_ids:
        return {}

    stmt = (
        select(  # type: ignore[call-overload]
            PlayerContentMention.content_id,
            PlayerMaster.id,  # type: ignore[union-attr]
            PlayerMaster.display_name,
            PlayerMaster.slug,
        )
        .join(
            PlayerMaster,
            PlayerMaster.id == PlayerContentMention.player_id,  # type: ignore[arg-type]
        )
        .where(
            PlayerContentMention.content_type == ContentType.PODCAST  # type: ignore[arg-type]
        )
        .where(
            PlayerContentMention.content_id.in_(episode_ids)  # type: ignore[attr-defined]
        )
        .order_by(PlayerMaster.display_name)
    )
    result = await db.execute(stmt)
    rows = result.all()

    mentions: dict[int, list[MentionedPlayer]] = {}
    for row in rows:
        content_id = row[0]
        player = MentionedPlayer(
            player_id=row[1],
            display_name=row[2] or "",
            slug=row[3] or "",
        )
        mentions.setdefault(content_id, []).append(player)
    return mentions


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
    tag: str | None = None,
    show_id: int | None = None,
) -> PodcastFeedResponse:
    """Fetch paginated podcast feed with joined show info.

    Args:
        db: Async database session
        limit: Maximum items to return
        offset: Number of items to skip
        tag: Optional tag value to filter by (e.g. "Mock Draft")
        show_id: Optional show ID to filter episodes by

    Returns:
        PodcastFeedResponse with items and pagination info
    """
    base_query = (
        select(*_PODCAST_FEED_COLUMNS)  # type: ignore[call-overload]
        .select_from(PodcastEpisode)
        .join(PodcastShow, PodcastShow.id == PodcastEpisode.show_id)  # type: ignore[arg-type]
    )

    count_query = select(func.count()).select_from(PodcastEpisode)

    if tag:
        tag_enum = _coerce_podcast_tag(tag)
        if tag_enum:
            tag_filter = PodcastEpisode.tag == tag_enum  # type: ignore[arg-type]
            base_query = base_query.where(tag_filter)  # type: ignore[arg-type]
            count_query = count_query.where(tag_filter)  # type: ignore[arg-type]

    if show_id is not None:
        show_filter = PodcastEpisode.show_id == show_id  # type: ignore[arg-type]
        base_query = base_query.where(show_filter)  # type: ignore[arg-type]
        count_query = count_query.where(show_filter)  # type: ignore[arg-type]

    count_result = await db.execute(count_query)
    total = count_result.scalar() or 0

    items_query = (
        base_query.order_by(PodcastEpisode.published_at.desc())  # type: ignore[attr-defined]
        .limit(limit)
        .offset(offset)
    )
    result = await db.execute(items_query)
    rows = result.mappings().all()

    episode_ids = [row["id"] for row in rows]
    mentions = await _load_mentions_for_episodes(db, episode_ids)
    items = [_row_to_episode_read(row, mentions=mentions) for row in rows]  # type: ignore[arg-type]

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

    episode_ids = [row["id"] for row in rows]
    mentions = await _load_mentions_for_episodes(db, episode_ids)
    return [_row_to_episode_read(row, mentions=mentions) for row in rows]  # type: ignore[arg-type]


async def get_player_podcast_feed(
    db: AsyncSession,
    player_id: int,
    limit: int = 20,
    offset: int = 0,
    min_items: int = 5,
) -> PodcastFeedResponse:
    """Fetch podcast feed for a specific player.

    Queries episodes where the player has a mention row (via
    PlayerContentMention with content_type=PODCAST) OR where
    PodcastEpisode.player_id matches directly. If results are below
    min_items, backfills with general podcast episodes (excluding
    already-included IDs).

    Args:
        db: Async database session
        player_id: Player to get episodes for
        limit: Maximum items to return
        offset: Number of items to skip
        min_items: Minimum items before backfilling with general feed

    Returns:
        PodcastFeedResponse with items marked with is_player_specific
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

    player_episode_ids: set[int] = set()
    episode_ids = [row["id"] for row in rows]
    mentions = await _load_mentions_for_episodes(db, episode_ids)
    items: list[PodcastEpisodeRead] = []

    for row in rows:
        player_episode_ids.add(row["id"])
        items.append(
            _row_to_episode_read(row, is_player_specific=True, mentions=mentions)  # type: ignore[arg-type]
        )

    # Backfill with general podcast feed if insufficient player-specific episodes
    if len(items) < min_items and offset == 0:
        backfill_needed = min_items - len(items)
        backfill_query = (
            select(*_PODCAST_FEED_COLUMNS)  # type: ignore[call-overload]
            .select_from(PodcastEpisode)
            .join(PodcastShow, PodcastShow.id == PodcastEpisode.show_id)  # type: ignore[arg-type]
            .order_by(PodcastEpisode.published_at.desc())  # type: ignore[attr-defined]
            .limit(backfill_needed)
        )

        if player_episode_ids:
            backfill_query = backfill_query.where(
                PodcastEpisode.id.notin_(list(player_episode_ids))  # type: ignore[union-attr]
            )

        backfill_result = await db.execute(backfill_query)
        backfill_rows = backfill_result.mappings().all()
        backfill_ids = [row["id"] for row in backfill_rows]
        backfill_mentions = await _load_mentions_for_episodes(db, backfill_ids)
        for row in backfill_rows:
            if len(items) >= limit:
                break
            items.append(
                _row_to_episode_read(
                    row,  # type: ignore[arg-type]
                    is_player_specific=False,
                    mentions=backfill_mentions,
                )
            )

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


async def get_podcast_page_data(
    db: AsyncSession,
    limit: int = 20,
    offset: int = 0,
    tag: str | None = None,
    show_id: int | None = None,
) -> dict[str, Any]:
    """Fetch all data needed for the /podcasts page in a single call.

    Args:
        db: Async database session
        limit: Maximum episodes to return
        offset: Number of episodes to skip
        tag: Optional tag value to filter by
        show_id: Optional show ID to filter episodes by

    Returns:
        Dict with keys: feed, shows, trending
    """
    feed = await get_podcast_feed(
        db, limit=limit, offset=offset, tag=tag, show_id=show_id
    )
    shows = await get_active_shows(db)
    trending: list[TrendingPlayer] = await get_trending_players(
        db, days=7, limit=7, content_type=ContentType.PODCAST
    )
    return {"feed": feed, "shows": shows, "trending": trending}


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
    row: dict[str, Any],
    is_player_specific: bool = False,
    mentions: dict[int, list[MentionedPlayer]] | None = None,
) -> PodcastEpisodeRead:
    """Convert a database row mapping to a PodcastEpisodeRead response model."""
    show_name = row["show_name"]
    episode_id: int = row["id"]
    return PodcastEpisodeRead(
        id=episode_id,
        show_name=show_name,
        artwork_url=row["artwork_url"],
        show_artwork_url=row["show_artwork_url"],
        title=row["title"],
        summary=row["summary"] or "",
        tag=_resolve_podcast_tag(row["tag"]),
        audio_url=row["audio_url"],
        episode_url=row["episode_url"],
        duration=format_duration(row["duration_seconds"]),
        time=format_relative_time(row["published_at"]),
        listen_on_text=build_listen_on_text(show_name),
        is_player_specific=is_player_specific,
        mentioned_players=(mentions or {}).get(episode_id, []),
    )
