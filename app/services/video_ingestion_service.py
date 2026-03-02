"""YouTube ingestion service for film-room videos."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Optional

import httpx
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.videos import VideoIngestionResult
from app.schemas.player_content_mentions import (
    ContentType,
    MentionSource,
    PlayerContentMention,
)
from app.schemas.players_master import PlayerMaster
from app.schemas.youtube_channels import YouTubeChannel
from app.schemas.youtube_videos import YouTubeVideo, YouTubeVideoTag
from app.services.player_mention_service import resolve_player_names_as_map
from app.services.video_service import (
    coerce_video_tag,
    parse_iso8601_duration,
    parse_youtube_video_id,
)
from app.services.video_summarization_service import video_summarization_service

logger = logging.getLogger(__name__)

_YOUTUBE_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=10.0)
_MAX_PAGES_PER_CHANNEL = 8
_SAFETY_BUFFER = timedelta(days=1)


@dataclass(frozen=True, slots=True)
class ChannelSnapshot:
    """Minimal channel data required for ingestion."""

    id: int
    name: str
    display_name: str
    channel_id: str
    uploads_playlist_id: str | None
    is_draft_focused: bool
    last_fetched_at: datetime | None


@dataclass(frozen=True, slots=True)
class RawVideo:
    """Minimal YouTube video metadata from API fetch phase."""

    external_id: str
    title: str
    description: str
    youtube_url: str
    thumbnail_url: str | None
    duration_seconds: int | None
    view_count: int | None
    published_at: datetime


async def run_ingestion_cycle(db: AsyncSession) -> VideoIngestionResult:
    """Ingest active YouTube channels into film-room videos."""
    api_key = settings.youtube_api_key
    if not api_key:
        return VideoIngestionResult(
            channels_processed=0,
            videos_added=0,
            videos_skipped=0,
            videos_filtered=0,
            mentions_added=0,
            errors=["YOUTUBE_API_KEY is not configured"],
        )

    channels = await _get_active_channel_snapshots(db)
    added = 0
    skipped = 0
    filtered = 0
    mentions = 0
    errors: list[str] = []

    for channel in channels:
        try:
            cutoff = (
                channel.last_fetched_at - _SAFETY_BUFFER
                if channel.last_fetched_at
                else datetime.now(UTC).replace(tzinfo=None) - timedelta(days=30)
            )
            raw_videos, uploads_playlist_id = await fetch_channel_videos(
                channel=channel,
                api_key=api_key,
                cutoff=cutoff,
            )
            rows: list[dict[str, Any]] = []
            mention_map: dict[str, list[str]] = {}
            now = datetime.now(UTC).replace(tzinfo=None)

            for raw in raw_videos:
                if not channel.is_draft_focused:
                    is_relevant = (
                        await video_summarization_service.check_draft_relevance(
                            raw.title,
                            raw.description,
                        )
                    )
                    if not is_relevant:
                        filtered += 1
                        continue

                try:
                    analysis = await video_summarization_service.analyze_video(
                        raw.title,
                        raw.description,
                    )
                    summary = analysis.summary
                    tag = analysis.tag
                    if analysis.mentioned_players:
                        mention_map[raw.external_id] = analysis.mentioned_players
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning(
                        f"Video analysis failed for '{raw.title[:50]}': {exc}"
                    )
                    summary = raw.description[:200] if raw.description else raw.title
                    tag = YouTubeVideoTag.SCOUTING_REPORT

                rows.append(
                    {
                        "channel_id": channel.id,
                        "external_id": raw.external_id,
                        "title": raw.title,
                        "description": raw.description or None,
                        "youtube_url": raw.youtube_url,
                        "thumbnail_url": raw.thumbnail_url,
                        "duration_seconds": raw.duration_seconds,
                        "view_count": raw.view_count,
                        "summary": summary or None,
                        "tag": tag,
                        "published_at": raw.published_at,
                        "created_at": now,
                        "is_manually_added": False,
                    }
                )

            inserted, conflict_skipped = await _persist_videos(
                db=db,
                channel_id=channel.id,
                rows=rows,
                uploads_playlist_id=uploads_playlist_id,
            )
            added += inserted
            skipped += conflict_skipped

            mentions_added = await _persist_ai_mentions(
                db=db,
                channel_id=channel.id,
                mention_map=mention_map,
                fetched_at=now,
            )
            mentions += mentions_added
        except Exception as exc:
            message = f"Failed channel {channel.display_name}: {exc}"
            logger.exception(message)
            errors.append(message)

    return VideoIngestionResult(
        channels_processed=len(channels),
        videos_added=added,
        videos_skipped=skipped,
        videos_filtered=filtered,
        mentions_added=mentions,
        errors=errors,
    )


async def add_video_by_url(
    db: AsyncSession,
    youtube_url: str,
    tag: str | None = None,
    player_ids: list[int] | None = None,
) -> int:
    """Add or update a single video by URL and reconcile MANUAL mentions."""
    api_key = settings.youtube_api_key
    if not api_key:
        raise ValueError("YOUTUBE_API_KEY is not configured")

    video_id = parse_youtube_video_id(youtube_url)
    if not video_id:
        raise ValueError("Invalid YouTube URL")

    raw = await _fetch_single_video(video_id=video_id, api_key=api_key)
    if raw is None:
        raise ValueError("Video not found")

    manual_player_ids = await _validate_manual_player_ids(db, player_ids or [])

    async with db.begin():
        channel = await _get_or_create_channel_for_manual_add(
            db=db,
            api_key=api_key,
            channel_external_id=raw["channel_id"],
            channel_name=raw["channel_title"],
        )
        if channel.id is None:
            raise RuntimeError("Failed to resolve channel id for manual add")

        now = datetime.now(UTC).replace(tzinfo=None)
        resolved_tag = coerce_video_tag(tag or "") or YouTubeVideoTag.SCOUTING_REPORT
        row = {
            "channel_id": channel.id,
            "external_id": video_id,
            "title": raw["title"],
            "description": raw["description"] or None,
            "youtube_url": f"https://www.youtube.com/watch?v={video_id}",
            "thumbnail_url": raw["thumbnail_url"],
            "duration_seconds": raw["duration_seconds"],
            "view_count": raw["view_count"],
            "summary": raw["description"][:200] if raw["description"] else raw["title"],
            "tag": resolved_tag,
            "published_at": raw["published_at"],
            "created_at": now,
            "is_manually_added": True,
        }

        stmt = (
            insert(YouTubeVideo)
            .values(row)
            .on_conflict_do_update(
                index_elements=[YouTubeVideo.external_id],
                set_={
                    "channel_id": row["channel_id"],
                    "title": row["title"],
                    "description": row["description"],
                    "youtube_url": row["youtube_url"],
                    "thumbnail_url": row["thumbnail_url"],
                    "duration_seconds": row["duration_seconds"],
                    "view_count": row["view_count"],
                    "summary": row["summary"],
                    "tag": row["tag"],
                    "published_at": row["published_at"],
                    "is_manually_added": True,
                },
            )
            .returning(YouTubeVideo.__table__.c.id)  # type: ignore[attr-defined]
        )
        video_db_id = int((await db.execute(stmt)).scalar_one())
        await _reconcile_manual_mentions(
            db,
            video_id=video_db_id,
            player_ids=manual_player_ids,
        )
    return video_db_id


async def reconcile_manual_mentions(
    db: AsyncSession,
    *,
    video_id: int,
    player_ids: list[int],
) -> int:
    """Upsert submitted MANUAL rows and remove stale MANUAL rows only."""
    valid_ids = await _validate_manual_player_ids(db, player_ids)
    async with db.begin():
        return await _reconcile_manual_mentions(
            db,
            video_id=video_id,
            player_ids=valid_ids,
        )


async def _reconcile_manual_mentions(
    db: AsyncSession,
    *,
    video_id: int,
    player_ids: list[int],
) -> int:
    """Reconcile MANUAL mentions for a video within an existing transaction."""
    video = await db.get(YouTubeVideo, video_id)
    published_at = video.published_at if video is not None else None  # type: ignore[union-attr]

    inserted = 0
    if player_ids:
        rows = [
            {
                "content_type": ContentType.VIDEO,
                "content_id": video_id,
                "player_id": player_id,
                "published_at": published_at,
                "source": MentionSource.MANUAL,
            }
            for player_id in player_ids
        ]
        stmt = (
            insert(PlayerContentMention)
            .values(rows)
            .on_conflict_do_nothing(constraint="uq_content_mention")
            .returning(PlayerContentMention.__table__.c.id)  # type: ignore[attr-defined]
        )
        inserted = len(list((await db.execute(stmt)).scalars().all()))

    delete_stmt = (
        delete(PlayerContentMention)
        .where(PlayerContentMention.content_type == ContentType.VIDEO)  # type: ignore[arg-type]
        .where(PlayerContentMention.content_id == video_id)  # type: ignore[arg-type]
        .where(PlayerContentMention.source == MentionSource.MANUAL)  # type: ignore[arg-type]
    )
    if player_ids:
        delete_stmt = delete_stmt.where(  # type: ignore[assignment]
            PlayerContentMention.player_id.notin_(player_ids)  # type: ignore[attr-defined]
        )
    await db.execute(delete_stmt)
    return inserted


async def _validate_manual_player_ids(
    db: AsyncSession,
    player_ids: list[int],
) -> list[int]:
    """Validate and normalize manual player IDs."""
    unique_ids = sorted({pid for pid in player_ids if pid > 0})
    if not unique_ids:
        return []

    valid_ids = set(
        (
            await db.execute(
                select(PlayerMaster.__table__.c.id).where(  # type: ignore[attr-defined]
                    PlayerMaster.__table__.c.id.in_(unique_ids)  # type: ignore[attr-defined]
                )
            )
        )
        .scalars()
        .all()
    )
    invalid_ids = sorted(set(unique_ids) - valid_ids)
    if invalid_ids:
        joined = ", ".join(str(item) for item in invalid_ids)
        raise ValueError(f"Invalid manual player ID(s): {joined}")
    return unique_ids


async def fetch_channel_videos(
    channel: ChannelSnapshot,
    api_key: str,
    cutoff: datetime,
) -> tuple[list[RawVideo], str | None]:
    """Fetch channel videos incrementally via uploads playlist pagination."""
    uploads_playlist_id = channel.uploads_playlist_id
    if not uploads_playlist_id:
        uploads_playlist_id = await _fetch_uploads_playlist_id(
            api_key=api_key,
            channel_id=channel.channel_id,
        )
    if not uploads_playlist_id:
        return [], None

    video_ids: list[str] = []
    page_token: str | None = None
    for _ in range(_MAX_PAGES_PER_CHANNEL):
        payload = await _youtube_get_with_retries(
            "https://www.googleapis.com/youtube/v3/playlistItems",
            {
                "part": "snippet,contentDetails",
                "playlistId": uploads_playlist_id,
                "maxResults": 50,
                "pageToken": page_token or "",
                "key": api_key,
            },
        )
        items = payload.get("items", [])
        stop_due_to_cutoff = False
        for item in items:
            published = _parse_datetime(
                item.get("contentDetails", {}).get("videoPublishedAt")
            )
            if published is not None and published <= cutoff:
                stop_due_to_cutoff = True
                continue
            vid = item.get("contentDetails", {}).get("videoId")
            if vid:
                video_ids.append(vid)
        page_token = payload.get("nextPageToken")
        if stop_due_to_cutoff or not page_token:
            break

    if not video_ids:
        return [], uploads_playlist_id

    unique_ids = list(dict.fromkeys(video_ids))
    raw_videos: list[RawVideo] = []
    for i in range(0, len(unique_ids), 50):
        chunk = unique_ids[i : i + 50]
        payload = await _youtube_get_with_retries(
            "https://www.googleapis.com/youtube/v3/videos",
            {
                "part": "snippet,contentDetails,statistics",
                "id": ",".join(chunk),
                "key": api_key,
            },
        )
        for item in payload.get("items", []):
            snippet = item.get("snippet", {})
            content_details = item.get("contentDetails", {})
            stats = item.get("statistics", {})
            published_at = _parse_datetime(snippet.get("publishedAt"))
            if published_at is None or published_at <= cutoff:
                continue
            raw_videos.append(
                RawVideo(
                    external_id=item.get("id", ""),
                    title=snippet.get("title", "Untitled"),
                    description=snippet.get("description", ""),
                    youtube_url=f"https://www.youtube.com/watch?v={item.get('id', '')}",
                    thumbnail_url=_pick_thumbnail(snippet.get("thumbnails", {})),
                    duration_seconds=parse_iso8601_duration(
                        content_details.get("duration")
                    ),
                    view_count=_to_int(stats.get("viewCount")),
                    published_at=published_at,
                )
            )
    return raw_videos, uploads_playlist_id


async def _get_active_channel_snapshots(db: AsyncSession) -> list[ChannelSnapshot]:
    stmt = select(YouTubeChannel).where(
        YouTubeChannel.is_active.is_(True)  # type: ignore[attr-defined]
    )
    channels = (await db.execute(stmt)).scalars().all()
    snapshots: list[ChannelSnapshot] = []
    for channel in channels:
        if channel.id is None:
            continue
        snapshots.append(
            ChannelSnapshot(
                id=channel.id,
                name=channel.name,
                display_name=channel.display_name,
                channel_id=channel.channel_id,
                uploads_playlist_id=channel.uploads_playlist_id,
                is_draft_focused=channel.is_draft_focused,
                last_fetched_at=channel.last_fetched_at,
            )
        )
    return snapshots


async def _persist_videos(
    db: AsyncSession,
    *,
    channel_id: int,
    rows: list[dict[str, Any]],
    uploads_playlist_id: str | None,
) -> tuple[int, int]:
    if not rows and uploads_playlist_id is None:
        return 0, 0
    async with db.begin():
        inserted = 0
        conflict_skipped = 0
        if rows:
            stmt = (
                insert(YouTubeVideo)
                .values(rows)
                .on_conflict_do_nothing(index_elements=[YouTubeVideo.external_id])
                .returning(YouTubeVideo.__table__.c.id)  # type: ignore[attr-defined]
            )
            inserted = len(list((await db.execute(stmt)).scalars().all()))
            conflict_skipped = len(rows) - inserted

        values: dict[str, Any] = {"updated_at": datetime.now(UTC).replace(tzinfo=None)}
        if uploads_playlist_id is not None:
            values["uploads_playlist_id"] = uploads_playlist_id
        await db.execute(
            update(YouTubeChannel)
            .where(YouTubeChannel.id == channel_id)  # type: ignore[arg-type]
            .values(**values)
        )
    return inserted, conflict_skipped


async def _persist_ai_mentions(
    db: AsyncSession,
    *,
    channel_id: int,
    mention_map: dict[str, list[str]],
    fetched_at: datetime,
) -> int:
    """Persist AI mentions and finalize channel watermark in one transaction."""
    async with db.begin():
        inserted = 0
        if mention_map:
            ext_ids = list(mention_map.keys())
            stmt = select(  # type: ignore[call-overload]
                YouTubeVideo.id,
                YouTubeVideo.external_id,
                YouTubeVideo.published_at,
            ).where(
                YouTubeVideo.channel_id == channel_id,  # type: ignore[arg-type]
                YouTubeVideo.external_id.in_(ext_ids),  # type: ignore[attr-defined]
            )
            ext_to_row: dict[str, tuple[int, datetime | None]] = {
                row[1]: (row[0], row[2])
                for row in (await db.execute(stmt)).all()  # type: ignore[misc]
            }
            all_names = list({name for names in mention_map.values() for name in names})
            name_to_player = await resolve_player_names_as_map(
                db, all_names, create_stubs=True
            )
            mention_rows: list[dict[str, Any]] = []
            seen: set[tuple[int, int]] = set()
            for ext_id, player_names in mention_map.items():
                item = ext_to_row.get(ext_id)
                if item is None:
                    continue
                video_id, published_at = item
                for name in player_names:
                    player_id = name_to_player.get(name.strip().lower())
                    if player_id is None:
                        continue
                    key = (video_id, player_id)
                    if key in seen:
                        continue
                    seen.add(key)
                    mention_rows.append(
                        {
                            "content_type": ContentType.VIDEO,
                            "content_id": video_id,
                            "player_id": player_id,
                            "published_at": published_at,
                            "source": MentionSource.AI,
                        }
                    )
            if mention_rows:
                insert_stmt = (
                    insert(PlayerContentMention)
                    .values(mention_rows)
                    .on_conflict_do_nothing(constraint="uq_content_mention")
                    .returning(PlayerContentMention.__table__.c.id)  # type: ignore[attr-defined]
                )
                inserted = len(list((await db.execute(insert_stmt)).scalars().all()))

        # Finalize watermark only after mention-phase succeeds.
        await db.execute(
            update(YouTubeChannel)
            .where(YouTubeChannel.id == channel_id)  # type: ignore[arg-type]
            .values(last_fetched_at=fetched_at, updated_at=fetched_at)
        )
    return inserted


async def _get_or_create_channel_for_manual_add(
    db: AsyncSession,
    *,
    api_key: str,
    channel_external_id: str,
    channel_name: str,
) -> YouTubeChannel:
    stmt = select(YouTubeChannel).where(
        YouTubeChannel.channel_id == channel_external_id  # type: ignore[arg-type]
    )
    existing = (await db.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        return existing

    uploads_playlist = await _fetch_uploads_playlist_id(
        api_key=api_key,
        channel_id=channel_external_id,
    )
    now = datetime.now(UTC).replace(tzinfo=None)
    channel = YouTubeChannel(
        name=channel_name,
        display_name=channel_name,
        channel_id=channel_external_id,
        channel_url=f"https://www.youtube.com/channel/{channel_external_id}",
        uploads_playlist_id=uploads_playlist,
        is_draft_focused=True,
        is_active=True,
        fetch_interval_minutes=60,
        created_at=now,
        updated_at=now,
    )
    db.add(channel)
    await db.flush()
    return channel


async def _fetch_uploads_playlist_id(
    *,
    api_key: str,
    channel_id: str,
) -> str | None:
    payload = await _youtube_get_with_retries(
        "https://www.googleapis.com/youtube/v3/channels",
        {
            "part": "contentDetails",
            "id": channel_id,
            "key": api_key,
        },
    )
    items = payload.get("items", [])
    if not items:
        return None
    return items[0].get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")


async def _fetch_single_video(*, video_id: str, api_key: str) -> dict[str, Any] | None:
    payload = await _youtube_get_with_retries(
        "https://www.googleapis.com/youtube/v3/videos",
        {
            "part": "snippet,contentDetails,statistics",
            "id": video_id,
            "key": api_key,
        },
    )
    items = payload.get("items", [])
    if not items:
        return None
    item = items[0]
    snippet = item.get("snippet", {})
    content_details = item.get("contentDetails", {})
    stats = item.get("statistics", {})
    return {
        "title": snippet.get("title", "Untitled"),
        "description": snippet.get("description", ""),
        "thumbnail_url": _pick_thumbnail(snippet.get("thumbnails", {})),
        "published_at": _parse_datetime(snippet.get("publishedAt"))
        or datetime.now(UTC).replace(tzinfo=None),
        "duration_seconds": parse_iso8601_duration(content_details.get("duration")),
        "view_count": _to_int(stats.get("viewCount")),
        "channel_id": snippet.get("channelId", ""),
        "channel_title": snippet.get("channelTitle", "YouTube"),
    }


async def _youtube_get_with_retries(
    url: str,
    params: dict[str, Any],
    *,
    retries: int = 3,
) -> dict[str, Any]:
    """Issue a YouTube API GET with bounded retry/backoff."""
    last_error: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            async with httpx.AsyncClient(timeout=_YOUTUBE_TIMEOUT) as client:
                response = await client.get(url, params=params)
            if response.status_code in {429, 500, 502, 503, 504}:
                raise httpx.HTTPStatusError(
                    f"Transient status {response.status_code}",
                    request=response.request,
                    response=response,
                )
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            last_error = exc if isinstance(exc, Exception) else Exception(str(exc))
            if attempt >= retries:
                break
            await asyncio.sleep(0.5 * (2**attempt))
    raise RuntimeError(f"YouTube API request failed: {last_error}")


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(UTC).replace(tzinfo=None)
    except ValueError:
        return None


def _pick_thumbnail(thumbnails: dict[str, Any]) -> str | None:
    for key in ("maxres", "standard", "high", "medium", "default"):
        entry = thumbnails.get(key)
        if isinstance(entry, dict) and entry.get("url"):
            return str(entry["url"])
    return None


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
