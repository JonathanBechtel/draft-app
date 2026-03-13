"""Film-room feed retrieval and formatting service."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from sqlalchemy import and_, func, or_, select
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.content_mentions import MentionedPlayer
from app.models.videos import VideoFeedResponse, YouTubeVideoRead
from app.schemas.player_content_mentions import ContentType, PlayerContentMention
from app.schemas.players_master import PlayerMaster
from app.schemas.youtube_channels import YouTubeChannel
from app.schemas.youtube_videos import YouTubeVideo, YouTubeVideoTag
from app.services.news_service import (
    TrendingPlayer,
    format_relative_time,
    get_trending_players,
)

# Videos shorter than this are excluded from public feeds (e.g. YouTube Shorts).
MIN_VIDEO_DURATION_SECONDS = 120

# Keep in sync with _row_to_video_read().
_VIDEO_FEED_COLUMNS = [
    YouTubeVideo.id,
    YouTubeVideo.title,
    YouTubeVideo.summary,
    YouTubeVideo.tag,
    YouTubeVideo.youtube_url,
    YouTubeVideo.thumbnail_url,
    YouTubeVideo.duration_seconds,
    YouTubeVideo.view_count,
    YouTubeVideo.published_at,
    YouTubeChannel.display_name.label("channel_name"),  # type: ignore[attr-defined]
]


def parse_youtube_video_id(youtube_url: str) -> str:
    """Extract a YouTube video id from common URL formats."""
    parsed = urlparse(youtube_url.strip())
    host = parsed.netloc.lower()
    path = parsed.path.strip("/")

    if "youtu.be" in host and path:
        return path.split("/")[0]

    if "youtube.com" in host:
        if path == "watch":
            video_id = parse_qs(parsed.query).get("v", [""])[0]
            return video_id
        if path.startswith("embed/"):
            return path.split("/", 1)[1]
        if path.startswith("shorts/"):
            return path.split("/", 1)[1]

    # Last fallback: raw id-like values.
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", youtube_url.strip()):
        return youtube_url.strip()
    return ""


def parse_iso8601_duration(value: str | None) -> Optional[int]:
    """Convert YouTube ISO-8601 duration (PT#H#M#S) to seconds."""
    if not value:
        return None
    match = re.fullmatch(
        r"PT(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?",
        value,
    )
    if not match:
        return None
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes") or 0)
    seconds = int(match.group("seconds") or 0)
    return hours * 3600 + minutes * 60 + seconds


def format_duration(seconds: int | None) -> str:
    """Format a duration in seconds."""
    if seconds is None or seconds < 0:
        return ""
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_view_count(view_count: int | None) -> str:
    """Format view count as a compact display value."""
    if view_count is None:
        return "—"
    if view_count < 1_000:
        return f"{view_count}"
    if view_count < 1_000_000:
        return f"{view_count / 1_000:.1f}K"
    return f"{view_count / 1_000_000:.1f}M"


def build_watch_on_text(channel_name: str) -> str:
    """Generate watch CTA text."""
    return f"Watch on {channel_name}"


def coerce_video_tag(raw: str) -> YouTubeVideoTag | None:
    """Parse a tag string that may be enum value or enum name."""
    try:
        return YouTubeVideoTag(raw)
    except ValueError:
        try:
            return YouTubeVideoTag[raw]
        except KeyError:
            return None


def resolve_video_tag(raw: str | YouTubeVideoTag) -> str:
    """Return display tag from enum/name/value input."""
    if isinstance(raw, YouTubeVideoTag):
        return raw.value
    try:
        return YouTubeVideoTag(raw).value
    except ValueError:
        try:
            return YouTubeVideoTag[raw].value
        except KeyError:
            return raw


async def _load_mentions_for_videos(
    db: AsyncSession,
    video_ids: list[int],
) -> dict[int, list[MentionedPlayer]]:
    """Batch-load player mentions for a set of videos."""
    if not video_ids:
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
        .where(PlayerContentMention.content_type == ContentType.VIDEO)  # type: ignore[arg-type]
        .where(PlayerContentMention.content_id.in_(video_ids))  # type: ignore[attr-defined]
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


def _row_to_video_read(
    row: RowMapping,
    *,
    is_player_specific: bool = False,
    mentions: dict[int, list[MentionedPlayer]] | None = None,
) -> YouTubeVideoRead:
    """Convert a query row mapping to `YouTubeVideoRead`."""
    video_id: int = row["id"]
    channel_name = row["channel_name"] or "YouTube"
    youtube_url = row["youtube_url"]
    return YouTubeVideoRead(
        id=video_id,
        channel_name=channel_name,
        thumbnail_url=row["thumbnail_url"],
        title=row["title"],
        summary=row["summary"] or "",
        tag=resolve_video_tag(row["tag"]),
        youtube_url=youtube_url,
        youtube_embed_id=parse_youtube_video_id(youtube_url),
        duration=format_duration(row["duration_seconds"]),
        time=format_relative_time(row["published_at"]),
        view_count_display=format_view_count(row["view_count"]),
        watch_on_text=build_watch_on_text(channel_name),
        is_player_specific=is_player_specific,
        mentioned_players=(mentions or {}).get(video_id, []),
    )


def _build_filtered_video_ids_query(
    *,
    tag: str | None = None,
    channel_id: int | None = None,
    player_id: int | None = None,
    search: str | None = None,
) -> Any:
    """Build a query for unique video IDs matching the current film-room filters."""
    duration_filter = (
        YouTubeVideo.duration_seconds >= MIN_VIDEO_DURATION_SECONDS  # type: ignore[operator]
    )
    stmt = (
        select(YouTubeVideo.id.label("video_id"))  # type: ignore[call-overload,union-attr]
        .select_from(YouTubeVideo)
        .where(duration_filter)  # type: ignore[arg-type]
    )

    if player_id is not None:
        stmt = stmt.join(
            PlayerContentMention,
            and_(
                PlayerContentMention.content_type == ContentType.VIDEO,  # type: ignore[arg-type]
                PlayerContentMention.content_id == YouTubeVideo.id,  # type: ignore[arg-type]
                PlayerContentMention.player_id == player_id,  # type: ignore[arg-type]
            ),
        )

    if tag:
        tag_enum = coerce_video_tag(tag)
        if tag_enum:
            stmt = stmt.where(YouTubeVideo.tag == tag_enum)  # type: ignore[arg-type]

    if channel_id is not None:
        stmt = stmt.where(YouTubeVideo.channel_id == channel_id)  # type: ignore[arg-type]

    if search:
        like_term = f"%{search.strip()}%"
        stmt = stmt.where(
            or_(
                YouTubeVideo.title.ilike(like_term),  # type: ignore[attr-defined]
                YouTubeVideo.summary.ilike(like_term),  # type: ignore[union-attr]
            )
        )

    return stmt.distinct()


async def get_video_feed(
    db: AsyncSession,
    limit: int = 20,
    offset: int = 0,
    tag: str | None = None,
    channel_id: int | None = None,
    player_id: int | None = None,
    search: str | None = None,
) -> VideoFeedResponse:
    """Fetch paginated film-room feed with optional filters."""
    filtered_video_ids = _build_filtered_video_ids_query(
        tag=tag,
        channel_id=channel_id,
        player_id=player_id,
        search=search,
    ).subquery()

    total_result = await db.execute(
        select(func.count()).select_from(filtered_video_ids)  # type: ignore[arg-type]
    )
    total = int(total_result.scalar() or 0)

    stmt = (
        select(*_VIDEO_FEED_COLUMNS)  # type: ignore[call-overload]
        .select_from(YouTubeVideo)
        .join(YouTubeChannel, YouTubeChannel.id == YouTubeVideo.channel_id)  # type: ignore[arg-type]
        .join(filtered_video_ids, filtered_video_ids.c.video_id == YouTubeVideo.id)
        .order_by(
            YouTubeVideo.published_at.desc(),  # type: ignore[attr-defined]
            YouTubeVideo.__table__.c.id.desc(),  # type: ignore[attr-defined]
        )
        .limit(limit)
        .offset(offset)
    )
    rows = (await db.execute(stmt)).mappings().all()

    video_ids = [row["id"] for row in rows]
    mentions = await _load_mentions_for_videos(db, video_ids)
    items = [
        _row_to_video_read(
            row, is_player_specific=player_id is not None, mentions=mentions
        )  # type: ignore[arg-type]
        for row in rows
    ]
    return VideoFeedResponse(items=items, total=total, limit=limit, offset=offset)


async def get_filtered_video_stats(
    db: AsyncSession,
    *,
    tag: str | None = None,
    channel_id: int | None = None,
    player_id: int | None = None,
    search: str | None = None,
    trending_days: int = 7,
    trending_limit: int = 7,
) -> dict[str, int]:
    """Return filtered channel and trending counts for film-room summary tiles."""
    filtered_video_ids = _build_filtered_video_ids_query(
        tag=tag,
        channel_id=channel_id,
        player_id=player_id,
        search=search,
    ).subquery()

    channel_total_result = await db.execute(
        select(func.count(func.distinct(YouTubeVideo.channel_id)))  # type: ignore[call-overload]
        .select_from(YouTubeVideo)
        .join(filtered_video_ids, filtered_video_ids.c.video_id == YouTubeVideo.id)
    )
    channel_total = int(channel_total_result.scalar() or 0)

    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
        days=trending_days
    )
    trending_total_result = await db.execute(
        select(func.count(func.distinct(PlayerContentMention.player_id)))  # type: ignore[call-overload]
        .select_from(PlayerContentMention)
        .join(
            filtered_video_ids,
            filtered_video_ids.c.video_id == PlayerContentMention.content_id,
        )
        .where(PlayerContentMention.content_type == ContentType.VIDEO)  # type: ignore[arg-type]
        .where(PlayerContentMention.published_at >= cutoff)  # type: ignore[arg-type,operator]
    )
    trending_total = int(trending_total_result.scalar() or 0)

    return {
        "channel_total": channel_total,
        "trending_total": min(trending_total, trending_limit),
    }


async def get_latest_videos_by_tag(
    db: AsyncSession,
    tag: str | None = None,
    limit: int = 6,
) -> list[YouTubeVideoRead]:
    """Fetch latest videos for homepage modules."""
    feed = await get_video_feed(db=db, limit=limit, offset=0, tag=tag)
    return feed.items


async def get_global_video_counts_by_tag(
    db: AsyncSession,
) -> dict[str, int]:
    """Return per-tag counts across all videos."""
    stmt = (
        select(  # type: ignore[call-overload]
            YouTubeVideo.tag,
            func.count().label("count"),
        )
        .where(YouTubeVideo.duration_seconds >= MIN_VIDEO_DURATION_SECONDS)  # type: ignore[operator]
        .group_by(YouTubeVideo.tag)
    )
    rows = (await db.execute(stmt)).all()
    counts: dict[str, int] = {}
    for row in rows:
        counts[resolve_video_tag(row[0])] = int(row[1])
    return counts


async def get_player_video_feed(
    db: AsyncSession,
    player_id: int,
    limit: int = 20,
    offset: int = 0,
) -> VideoFeedResponse:
    """Fetch videos linked to a player via canonical mention rows."""
    return await get_video_feed(
        db=db,
        player_id=player_id,
        limit=limit,
        offset=offset,
    )


async def get_player_video_counts_by_tag(
    db: AsyncSession,
    player_id: int,
) -> dict[str, int]:
    """Return per-tag counts for player-specific videos."""
    stmt = (
        select(  # type: ignore[call-overload]
            YouTubeVideo.tag,
            func.count().label("count"),
        )
        .join(
            PlayerContentMention,
            and_(
                PlayerContentMention.content_type == ContentType.VIDEO,  # type: ignore[arg-type]
                PlayerContentMention.content_id == YouTubeVideo.id,  # type: ignore[arg-type]
                PlayerContentMention.player_id == player_id,  # type: ignore[arg-type]
            ),
        )
        .where(YouTubeVideo.duration_seconds >= MIN_VIDEO_DURATION_SECONDS)  # type: ignore[operator]
        .group_by(YouTubeVideo.tag)
    )
    rows = (await db.execute(stmt)).all()
    counts: dict[str, int] = {}
    for row in rows:
        counts[resolve_video_tag(row[0])] = int(row[1])
    return counts


async def get_video_page_data(
    db: AsyncSession,
    limit: int = 20,
    offset: int = 0,
    tag: str | None = None,
    channel_id: int | None = None,
    player_id: int | None = None,
    search: str | None = None,
) -> dict[str, Any]:
    """Fetch film-room page data in a single service call."""
    feed = await get_video_feed(
        db=db,
        limit=limit,
        offset=offset,
        tag=tag,
        channel_id=channel_id,
        player_id=player_id,
        search=search,
    )
    stats = await get_filtered_video_stats(
        db=db,
        tag=tag,
        channel_id=channel_id,
        player_id=player_id,
        search=search,
    )
    channels = await get_active_channels(db)
    trending: list[TrendingPlayer] = await get_trending_players(
        db, days=7, limit=7, content_type=ContentType.VIDEO
    )
    return {"feed": feed, "channels": channels, "trending": trending, "stats": stats}


async def get_video_player_filters(
    db: AsyncSession,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return top players mentioned in video content for filter UI."""
    stmt = (
        select(  # type: ignore[call-overload]
            PlayerMaster.id,
            PlayerMaster.display_name,
            PlayerMaster.slug,
            func.count().label("mention_count"),
        )
        .join(
            PlayerContentMention,
            PlayerContentMention.player_id == PlayerMaster.id,  # type: ignore[arg-type]
        )
        .where(PlayerContentMention.content_type == ContentType.VIDEO)  # type: ignore[arg-type]
        .group_by(PlayerMaster.id, PlayerMaster.display_name, PlayerMaster.slug)
        .order_by(func.count().desc(), PlayerMaster.display_name)  # type: ignore[arg-type,call-overload]
        .limit(limit)
    )
    rows = (await db.execute(stmt)).all()
    return [
        {
            "id": row[0],
            "display_name": row[1] or "",
            "slug": row[2] or "",
            "mention_count": int(row[3]),
        }
        for row in rows
    ]


async def get_active_channels(db: AsyncSession) -> list[YouTubeChannel]:
    """Fetch active YouTube channels that have at least one eligible video."""
    channels_with_videos = (
        select(YouTubeVideo.channel_id)  # type: ignore[call-overload]
        .where(YouTubeVideo.duration_seconds >= MIN_VIDEO_DURATION_SECONDS)  # type: ignore[operator]
        .group_by(YouTubeVideo.channel_id)
    )
    stmt = (
        select(YouTubeChannel)
        .where(YouTubeChannel.is_active.is_(True))  # type: ignore[attr-defined]
        .where(YouTubeChannel.id.in_(channels_with_videos))  # type: ignore[union-attr]
    )
    return list((await db.execute(stmt)).scalars().all())
