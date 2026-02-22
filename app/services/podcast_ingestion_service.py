"""Podcast feed ingestion service.

Handles fetching, parsing, filtering, and storing podcast episodes
from RSS feeds. Mirrors news_ingestion_service.py with podcast-specific
relevance filtering and field mapping.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Optional

import httpx
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.podcasts import PodcastIngestionResult
from app.schemas.player_content_mentions import (
    ContentType,
    MentionSource,
    PlayerContentMention,
)
from app.schemas.podcast_episodes import PodcastEpisode, PodcastEpisodeTag
from app.schemas.podcast_shows import PodcastShow
from app.schemas.players_master import PlayerMaster  # noqa: F401 - needed for FK resolution
from app.services.player_mention_service import resolve_player_names_as_map
from app.services.podcast_service import get_active_shows
from app.services.podcast_summarization_service import podcast_summarization_service

logger = logging.getLogger(__name__)

_RSS_USER_AGENT = "DraftGuru/1.0 (+https://draftguru)"
_RSS_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)

_TRANSIENT_DB_ERROR_MARKERS = (
    "cache lookup failed for type",
    "InvalidCachedStatementError",
    "cached statement plan is invalid",
    "ConnectionDoesNotExistError",
    "connection was closed",
    "closed in the middle of operation",
)

DRAFT_RELEVANCE_KEYWORDS = [
    "nba draft",
    "mock draft",
    "draft prospect",
    "draft board",
    "draft pick",
    "draft lottery",
    "draft combine",
    "combine",
    "prospect",
    "big board",
    "draft class",
    "draft night",
    "lottery pick",
    "top pick",
    "draft stock",
    "draft order",
]


@dataclass(frozen=True, slots=True)
class PodcastShowSnapshot:
    """Minimal show data needed to ingest a feed without ORM lazy loads."""

    id: int
    name: str
    feed_url: str
    is_draft_focused: bool
    last_fetched_at: datetime | None


async def run_ingestion_cycle(db: AsyncSession) -> PodcastIngestionResult:
    """Process all active podcast shows.

    Args:
        db: Async database session

    Returns:
        PodcastIngestionResult with counts and any errors
    """
    async with db.begin():
        active_shows = await get_active_shows(db)
        show_snapshots: list[PodcastShowSnapshot] = []
        for s in active_shows:
            if s.id is None:
                logger.warning(f"Skipping show without ID: {s.name}")
                continue
            show_snapshots.append(
                PodcastShowSnapshot(
                    id=s.id,
                    name=s.name,
                    feed_url=s.feed_url,
                    is_draft_focused=s.is_draft_focused,
                    last_fetched_at=s.last_fetched_at,
                )
            )

    logger.info(f"Starting podcast ingestion: {len(show_snapshots)} active show(s)")

    total_added = 0
    total_skipped = 0
    total_filtered = 0
    total_mentions = 0
    errors: list[str] = []

    for show in show_snapshots:
        try:
            added, skipped, filtered, mentions = await ingest_podcast_show(db, show)
            total_added += added
            total_skipped += skipped
            total_filtered += filtered
            total_mentions += mentions
        except Exception as e:
            error_msg = f"Failed to ingest {show.name}: {str(e)}"
            logger.error(error_msg)
            errors.append(error_msg)

    logger.info(
        f"Podcast ingestion complete: {total_added} added, {total_skipped} skipped, "
        f"{total_filtered} filtered, {total_mentions} mentions, {len(errors)} error(s)"
    )

    return PodcastIngestionResult(
        shows_processed=len(show_snapshots),
        episodes_added=total_added,
        episodes_skipped=total_skipped,
        episodes_filtered=total_filtered,
        mentions_added=total_mentions,
        errors=errors,
    )


async def ingest_podcast_show(
    db: AsyncSession,
    show: PodcastShowSnapshot,
) -> tuple[int, int, int, int]:
    """Fetch and process a single podcast show's RSS feed.

    Args:
        db: Async database session
        show: Show snapshot to ingest

    Returns:
        Tuple of (episodes_added, episodes_skipped, episodes_filtered, mentions_added)
    """
    logger.info(f"-> {show.name}")

    entries = await fetch_podcast_rss_feed(show.feed_url)
    logger.info(f"  Fetched {len(entries)} entries from feed")

    # Skip entries older than last fetch (with 1-day buffer for late-arriving items)
    if show.last_fetched_at is not None:
        cutoff = show.last_fetched_at - timedelta(days=1)
        entries = [e for e in entries if e.get("published_at", datetime.min) >= cutoff]
        logger.info(
            f"  After date filter (since {cutoff.isoformat()}): {len(entries)} entries"
        )

    episodes_skipped = 0

    # Deduplicate within feed
    seen_ids: set[str] = set()
    candidates: list[dict[str, Any]] = []
    for entry in entries:
        external_id = entry.get("guid", "")
        if not external_id:
            logger.warning(
                f"Skipping entry without ID: {entry.get('title', 'unknown')}"
            )
            episodes_skipped += 1
            continue
        if external_id in seen_ids:
            episodes_skipped += 1
            continue
        seen_ids.add(external_id)
        candidates.append(entry)

    # Filter already-existing episodes
    async with db.begin():
        existing_ids = await _fetch_existing_external_ids(
            db,
            show_id=show.id,
            external_ids=[entry.get("guid", "") for entry in candidates],
        )

    new_entries = [
        entry for entry in candidates if entry.get("guid", "") not in existing_ids
    ]
    episodes_skipped += len(candidates) - len(new_entries)

    # Phase 1: network/AI work (no DB held)
    fetched_at = datetime.now(UTC).replace(tzinfo=None)
    rows: list[dict[str, Any]] = []
    mention_map: dict[str, list[str]] = {}
    episodes_filtered = 0

    for entry in new_entries:
        external_id = entry.get("guid", "")
        if not external_id:
            episodes_skipped += 1
            continue

        title = entry.get("title", "Untitled")
        description = entry.get("description", "")

        # Relevance gate
        if not show.is_draft_focused:
            if not check_keyword_relevance(title, description):
                is_relevant = await podcast_summarization_service.check_draft_relevance(
                    title, description
                )
                if not is_relevant:
                    episodes_filtered += 1
                    logger.debug(f"  Filtered (not relevant): {title[:60]}")
                    continue

        # AI analysis on relevant episodes
        mentioned_players: list[str] = []
        try:
            analysis = await podcast_summarization_service.analyze_episode(
                title=title,
                description=description,
            )
            summary = analysis.summary
            tag = analysis.tag
            mentioned_players = analysis.mentioned_players
        except Exception as e:
            logger.warning(f"AI analysis failed for '{title[:30]}': {e}")
            summary = description[:200] if description else ""
            tag = PodcastEpisodeTag.DRAFT_ANALYSIS

        audio_url = entry.get("audio_url", "")
        if not audio_url:
            logger.warning(f"  Skipping entry without audio URL: {title[:60]}")
            episodes_skipped += 1
            continue

        rows.append(
            {
                "show_id": show.id,
                "external_id": external_id,
                "title": title,
                "description": description or None,
                "audio_url": audio_url,
                "duration_seconds": entry.get("duration_seconds"),
                "episode_url": entry.get("episode_url"),
                "artwork_url": entry.get("artwork_url"),
                "season": entry.get("season"),
                "episode_number": entry.get("episode_number"),
                "summary": summary or None,
                "tag": tag,
                "published_at": entry.get(
                    "published_at", datetime.now(UTC).replace(tzinfo=None)
                ),
                "created_at": fetched_at,
                "player_id": None,
            }
        )
        if mentioned_players:
            mention_map[external_id] = mentioned_players
        logger.info(f"  + [{tag.value}] {title[:60]}{'...' if len(title) > 60 else ''}")

    # Phase 2: persist episodes
    inserted, conflict_skipped = await _persist_podcast_episodes(
        db,
        show_id=show.id,
        rows=rows,
        fetched_at=fetched_at,
    )
    episodes_added = inserted
    episodes_skipped += conflict_skipped

    # Phase 3: persist player mentions (best-effort)
    mentions_added = 0
    if mention_map:
        try:
            mentions_added = await _persist_player_mentions(
                db, show_id=show.id, mention_map=mention_map
            )
        except Exception as e:
            logger.warning(f"  Player mention persistence failed: {e}")

    logger.info(
        f"  {show.name}: {episodes_added} added, {episodes_skipped} skipped, "
        f"{episodes_filtered} filtered, {mentions_added} mentions"
    )
    return episodes_added, episodes_skipped, episodes_filtered, mentions_added


def check_keyword_relevance(title: str, description: str) -> bool:
    """Check if episode title or description contains draft-related keywords.

    Pure function — no API calls. Used as the first-pass relevance filter
    before the Gemini relevance check.

    Args:
        title: Episode title
        description: Episode description

    Returns:
        True if any draft keyword is found in title or description
    """
    text = f"{title} {description}".lower()
    return any(keyword in text for keyword in DRAFT_RELEVANCE_KEYWORDS)


async def fetch_podcast_rss_feed(url: str) -> list[dict[str, Any]]:
    """Parse a podcast RSS feed and extract normalized episode fields.

    Uses feedparser to handle various RSS/Atom formats with
    podcast-specific field mapping (itunes:*, enclosure audio).

    Args:
        url: RSS feed URL to fetch

    Returns:
        List of normalized entry dictionaries
    """
    if not url.startswith(("http://", "https://")):
        logger.warning(f"Skipping non-http(s) feed URL: {url}")
        return []

    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": _RSS_USER_AGENT},
            timeout=_RSS_TIMEOUT,
            follow_redirects=True,
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            content = response.content
    except httpx.HTTPError as exc:
        logger.warning(f"Failed to fetch feed {url}: {exc}")
        return []

    try:
        import feedparser  # type: ignore[import-untyped]
    except ModuleNotFoundError:
        raise RuntimeError(
            "feedparser is required for podcast ingestion. "
            "Install it with: pip install feedparser"
        )

    feed = await asyncio.to_thread(feedparser.parse, content)

    if feed.bozo:
        logger.warning(f"Feed parse warning for {url}: {feed.bozo_exception}")

    entries: list[dict[str, Any]] = []
    for entry in feed.entries:
        audio_url = _extract_audio_url(entry)
        artwork_url = _extract_podcast_artwork(entry)
        published_at = _parse_published_date(entry)
        duration_seconds = _parse_itunes_duration(entry)

        entries.append(
            {
                "title": entry.get("title", ""),
                "description": _clean_description(
                    entry.get("summary", entry.get("description", ""))
                ),
                "guid": entry.get("id", entry.get("link", "")),
                "audio_url": audio_url,
                "episode_url": entry.get("link", ""),
                "artwork_url": artwork_url,
                "duration_seconds": duration_seconds,
                "season": _parse_int_field(entry, "itunes_season"),
                "episode_number": _parse_int_field(entry, "itunes_episode"),
                "published_at": published_at,
            }
        )

    return entries


def _extract_audio_url(entry: dict[str, Any]) -> Optional[str]:
    """Extract audio URL from RSS entry enclosures.

    Args:
        entry: feedparser entry dict

    Returns:
        Audio URL if found, None otherwise
    """
    enclosures = entry.get("enclosures", entry.get("links", []))
    for enclosure in enclosures:
        enc_type = enclosure.get("type", "")
        if enc_type.startswith("audio/"):
            return enclosure.get("href", enclosure.get("url"))
    return None


def _extract_podcast_artwork(entry: dict[str, Any]) -> Optional[str]:
    """Extract episode artwork from RSS entry.

    Checks itunes:image, media:content, and media:thumbnail.

    Args:
        entry: feedparser entry dict

    Returns:
        Artwork URL if found, None otherwise
    """
    # Check itunes:image (most podcast feeds use this)
    itunes_image = entry.get("image", {})
    if isinstance(itunes_image, dict) and itunes_image.get("href"):
        return itunes_image["href"]

    # Check media content
    media_content = entry.get("media_content", [])
    for media in media_content:
        medium = media.get("medium", "")
        media_type = media.get("type", "")
        if medium == "image" or media_type.startswith("image/"):
            return media.get("url")

    # Check media thumbnail
    media_thumbnail = entry.get("media_thumbnail", [])
    if media_thumbnail:
        return media_thumbnail[0].get("url")

    return None


def _parse_itunes_duration(entry: dict[str, Any]) -> Optional[int]:
    """Parse itunes:duration field to seconds.

    Handles formats: HH:MM:SS, MM:SS, raw seconds string.

    Args:
        entry: feedparser entry dict

    Returns:
        Duration in seconds, or None if not parseable
    """
    raw = entry.get("itunes_duration", "")
    if not raw:
        return None

    raw = str(raw).strip()
    if not raw:
        return None

    # Try raw seconds (integer string)
    if raw.isdigit():
        return int(raw)

    # Try HH:MM:SS or MM:SS
    parts = raw.split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        elif len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
    except (ValueError, IndexError):
        pass

    return None


def _parse_int_field(entry: dict[str, Any], field: str) -> Optional[int]:
    """Parse an integer field from a feedparser entry.

    Args:
        entry: feedparser entry dict
        field: Field name to extract

    Returns:
        Integer value or None if not parseable
    """
    raw = entry.get(field)
    if raw is None:
        return None
    try:
        return int(raw)
    except (ValueError, TypeError):
        return None


def _parse_published_date(entry: dict[str, Any]) -> datetime:
    """Parse published date from RSS entry.

    Tries multiple date fields and formats. Returns naive UTC datetime.

    Args:
        entry: feedparser entry dict

    Returns:
        Parsed datetime (naive UTC) or current time if parsing fails
    """
    if hasattr(entry, "published_parsed") and entry.published_parsed:
        try:
            return datetime(*entry.published_parsed[:6])
        except Exception:
            pass

    published = entry.get("published", entry.get("pubDate", ""))
    if published:
        try:
            dt = parsedate_to_datetime(published)
            return dt.replace(tzinfo=None)
        except Exception:
            pass

    return datetime.now(UTC).replace(tzinfo=None)


def _clean_description(description: str) -> str:
    """Clean HTML from description text.

    Args:
        description: Raw description with possible HTML

    Returns:
        Cleaned text
    """
    if not description:
        return ""

    text = re.sub(r"<[^>]+>", "", description)
    text = re.sub(r"\s+", " ", text)
    text = text.replace("&amp;", "&")
    text = text.replace("&lt;", "<")
    text = text.replace("&gt;", ">")
    text = text.replace("&quot;", '"')
    text = text.replace("&#39;", "'")
    text = text.replace("&nbsp;", " ")

    return text.strip()


async def _fetch_existing_external_ids(
    db: AsyncSession,
    *,
    show_id: int,
    external_ids: list[str],
) -> set[str]:
    """Fetch external_ids that already exist for a show."""
    if not external_ids:
        return set()

    stmt = select(PodcastEpisode.external_id).where(  # type: ignore[call-overload]
        PodcastEpisode.show_id == show_id,  # type: ignore[arg-type]
        PodcastEpisode.external_id.in_(external_ids),  # type: ignore[attr-defined,arg-type]
    )
    result = await db.execute(stmt)
    return set(result.scalars().all())


def _is_transient_db_error(exc: BaseException) -> bool:
    """Return True when the DB exception is likely fixed by retrying once."""
    if isinstance(exc, DBAPIError) and exc.connection_invalidated:
        return True
    text = str(exc)
    return any(marker in text for marker in _TRANSIENT_DB_ERROR_MARKERS)


async def _persist_podcast_episodes(
    db: AsyncSession,
    *,
    show_id: int,
    rows: list[dict[str, Any]],
    fetched_at: datetime,
) -> tuple[int, int]:
    """Insert new episodes idempotently and touch show timestamps.

    Uses ON CONFLICT DO NOTHING on the unique constraint.
    """

    async def _attempt() -> tuple[int, int]:
        async with db.begin():
            inserted_count = 0
            conflict_skipped = 0
            if rows:
                stmt = (
                    insert(PodcastEpisode)
                    .values(rows)
                    .on_conflict_do_nothing(
                        constraint="uq_podcast_episodes_show_external"
                    )
                    .returning(PodcastEpisode.__table__.c.id)  # type: ignore[attr-defined]
                )
                result = await db.execute(stmt)
                inserted_ids = list(result.scalars().all())
                inserted_count = len(inserted_ids)
                conflict_skipped = len(rows) - inserted_count

            await db.execute(
                update(PodcastShow)
                .where(PodcastShow.id == show_id)  # type: ignore[arg-type]
                .values(last_fetched_at=fetched_at, updated_at=fetched_at)
            )

            return inserted_count, conflict_skipped

    try:
        return await _attempt()
    except Exception as exc:
        if _is_transient_db_error(exc):
            logger.warning("Transient DB error during podcast ingest; retrying once")
            return await _attempt()
        raise


async def _persist_player_mentions(
    db: AsyncSession,
    *,
    show_id: int,
    mention_map: dict[str, list[str]],
) -> int:
    """Resolve AI-detected player names and insert mention rows.

    Args:
        db: Async database session
        show_id: ID of the podcast show being ingested
        mention_map: Map of external_id -> list of player names

    Returns:
        Number of mention rows actually inserted
    """
    if not mention_map:
        return 0

    total_inserted = 0
    async with db.begin():
        # 1. Fetch the PodcastEpisode IDs + published_at for the external_ids
        external_ids = list(mention_map.keys())
        stmt = select(  # type: ignore[call-overload]
            PodcastEpisode.id, PodcastEpisode.external_id, PodcastEpisode.published_at
        ).where(
            PodcastEpisode.show_id == show_id,  # type: ignore[arg-type]
            PodcastEpisode.external_id.in_(external_ids),  # type: ignore[attr-defined,arg-type]
        )
        result = await db.execute(stmt)
        ext_to_item: dict[str, tuple[int, datetime | None]] = {
            row[1]: (row[0], row[2])
            for row in result.all()  # type: ignore[misc]
        }

        # 2. Collect all unique player names and resolve to IDs
        all_names: list[str] = list(
            {n for names in mention_map.values() for n in names}
        )
        name_to_player_id = await resolve_player_names_as_map(
            db, all_names, create_stubs=True
        )

        # 3. Build mention rows
        mention_rows: list[dict[str, Any]] = []
        seen: set[tuple[int, int]] = set()
        for ext_id, player_names in mention_map.items():
            item_data = ext_to_item.get(ext_id)
            if item_data is None:
                continue
            episode_id, published_at = item_data
            for pname in player_names:
                player_id = name_to_player_id.get(pname.strip().lower())
                if player_id is None:
                    continue
                key = (episode_id, player_id)
                if key in seen:
                    continue
                seen.add(key)
                mention_rows.append(
                    {
                        "content_type": ContentType.PODCAST,
                        "content_id": episode_id,
                        "player_id": player_id,
                        "published_at": published_at,
                        "source": MentionSource.AI,
                    }
                )

        # 4. Bulk insert with conflict handling
        if mention_rows:
            stmt_insert = (
                insert(PlayerContentMention)
                .values(mention_rows)
                .on_conflict_do_nothing(constraint="uq_content_mention")
                .returning(PlayerContentMention.__table__.c.id)  # type: ignore[attr-defined]
            )
            insert_result = await db.execute(stmt_insert)
            total_inserted = len(list(insert_result.scalars().all()))

    logger.info(f"  Persisted {total_inserted} player mentions")
    return total_inserted
