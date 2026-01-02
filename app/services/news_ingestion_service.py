"""News feed ingestion service.

Handles fetching, parsing, and storing news from various feed types.
Currently supports RSS feeds with architecture for future expansion.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Optional

import feedparser
import httpx
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.news import IngestionResult
from app.schemas.news_items import NewsItem
from app.schemas.news_sources import FeedType, NewsSource
from app.schemas.players_master import PlayerMaster  # noqa: F401 - needed for FK resolution
from app.services.news_service import get_active_sources
from app.services.news_summarization_service import news_summarization_service

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


@dataclass(frozen=True, slots=True)
class NewsSourceSnapshot:
    """Minimal source data needed to ingest a feed without ORM lazy loads."""

    id: int
    name: str
    feed_type: FeedType
    feed_url: str


async def run_ingestion_cycle(db: AsyncSession) -> IngestionResult:
    """Process all active sources based on their feed type.

    Iterates through all active sources and dispatches to the
    appropriate ingestion handler based on feed_type.

    Args:
        db: Async database session

    Returns:
        IngestionResult with counts and any errors
    """
    active_sources = await get_active_sources(db)
    source_snapshots: list[NewsSourceSnapshot] = []
    for active_source in active_sources:
        if active_source.id is None:
            logger.warning(f"Skipping source without ID: {active_source.name}")
            continue
        source_snapshots.append(
            NewsSourceSnapshot(
                id=active_source.id,
                name=active_source.name,
                feed_type=active_source.feed_type,
                feed_url=active_source.feed_url,
            )
        )

    # End the implicit transaction opened by the SELECT so we never hold a DB
    # connection open while doing network/AI work.
    await db.commit()

    sources: list[NewsSourceSnapshot] = source_snapshots
    logger.info(f"Starting ingestion cycle: {len(sources)} active source(s)")

    total_added = 0
    total_skipped = 0
    errors: list[str] = []

    for source in sources:
        try:
            if source.feed_type == FeedType.RSS:
                added, skipped = await ingest_rss_source(db, source)
                total_added += added
                total_skipped += skipped
            # Future: elif source.feed_type == FeedType.API: ...
            else:
                logger.warning(
                    f"Unknown feed type for source {source.name}: {source.feed_type}"
                )
                errors.append(f"Unknown feed type: {source.feed_type}")
        except Exception as e:
            error_msg = f"Failed to ingest {source.name}: {str(e)}"
            logger.error(error_msg)
            errors.append(error_msg)

    logger.info(
        f"Ingestion complete: {total_added} added, {total_skipped} skipped, "
        f"{len(errors)} error(s)"
    )

    return IngestionResult(
        sources_processed=len(sources),
        items_added=total_added,
        items_skipped=total_skipped,
        errors=errors,
    )


async def ingest_rss_source(
    db: AsyncSession,
    source: NewsSourceSnapshot,
) -> tuple[int, int]:
    """Fetch and process an RSS feed source.

    Parses the RSS feed, generates AI summaries, and inserts new items
    with deduplication based on external_id.

    Args:
        db: Async database session
        source: NewsSource record to ingest

    Returns:
        Tuple of (items_added, items_skipped)
    """
    logger.info(f"→ {source.name}")

    entries = await fetch_rss_feed(source.feed_url)
    logger.info(f"  Fetched {len(entries)} entries")

    items_added = 0
    items_skipped = 0

    seen_ids: set[str] = set()
    candidates: list[dict] = []
    for entry in entries:
        external_id = entry.get("guid", entry.get("link", ""))
        if not external_id:
            logger.warning(
                f"Skipping entry without ID: {entry.get('title', 'unknown')}"
            )
            items_skipped += 1
            continue
        if external_id in seen_ids:
            items_skipped += 1
            continue
        seen_ids.add(external_id)
        candidates.append(entry)

    existing_ids = await _fetch_existing_external_ids(
        db,
        source_id=source.id,
        external_ids=[entry.get("guid", "") for entry in candidates],
    )
    await db.commit()

    new_entries = [
        entry for entry in candidates if entry.get("guid", "") not in existing_ids
    ]
    items_skipped += len(candidates) - len(new_entries)

    # Phase 1: network/AI work (no DB connections/transactions held).
    fetched_at = datetime.utcnow()
    rows: list[dict] = []
    for entry in new_entries:
        external_id = entry.get("guid", entry.get("link", ""))
        if not external_id:
            # Should be impossible due to the candidate filtering above, but keep this
            # defensive in case the feed mapping changes.
            items_skipped += 1
            continue

        # Extract fields from RSS entry
        title = entry.get("title", "Untitled")
        description = entry.get("description", "")
        url = entry.get("link", "")
        image_url = entry.get("image_url")
        author = entry.get("author")
        published_at = entry.get("published_at", datetime.utcnow())

        # Generate AI summary and tag
        try:
            analysis = await news_summarization_service.analyze_article(
                title=title,
                description=description,
            )
            summary = analysis.summary
            tag = analysis.tag
        except Exception as e:
            logger.warning(f"AI analysis failed for '{title[:30]}': {e}")
            summary = description[:200] if description else ""
            from app.schemas.news_items import NewsItemTag

            tag = NewsItemTag.SCOUTING_REPORT

        rows.append(
            {
                "source_id": source.id,
                "external_id": external_id,
                "title": title,
                "description": description or None,
                "url": url,
                "image_url": image_url,
                "author": author,
                "summary": summary or None,
                "tag": tag,
                "published_at": published_at,
                "created_at": fetched_at,
                "player_id": None,
            }
        )
        logger.info(f"  + [{tag.value}] {title[:60]}{'...' if len(title) > 60 else ''}")

    # Phase 2: short DB transaction to insert + update timestamps.
    inserted, conflict_skipped = await _persist_news_items(
        db,
        source_id=source.id,
        rows=rows,
        fetched_at=fetched_at,
    )
    items_added += inserted
    items_skipped += conflict_skipped

    logger.info(f"  ✓ {source.name}: {items_added} added, {items_skipped} skipped")
    return items_added, items_skipped


async def _fetch_existing_external_ids(
    db: AsyncSession,
    *,
    source_id: int,
    external_ids: list[str],
) -> set[str]:
    """Fetch IDs that already exist for a source."""

    if not external_ids:
        return set()

    stmt = select(NewsItem.external_id).where(  # type: ignore[call-overload]
        NewsItem.source_id == source_id,  # type: ignore[arg-type]
        NewsItem.external_id.in_(external_ids),  # type: ignore[attr-defined,arg-type]
    )
    result = await db.execute(stmt)
    return set(result.scalars().all())


def _is_transient_db_error(exc: BaseException) -> bool:
    """Return True when the DB exception is likely fixed by retrying once."""

    if isinstance(exc, DBAPIError) and exc.connection_invalidated:
        return True
    text = str(exc)
    return any(marker in text for marker in _TRANSIENT_DB_ERROR_MARKERS)


async def _persist_news_items(
    db: AsyncSession,
    *,
    source_id: int,
    rows: list[dict],
    fetched_at: datetime,
) -> tuple[int, int]:
    """Insert new items idempotently and touch source timestamps.

    Uses ON CONFLICT DO NOTHING so the unique constraint is the source of truth.
    """

    async def _attempt() -> tuple[int, int]:
        inserted_count = 0
        conflict_skipped = 0
        if rows:
            stmt = (
                insert(NewsItem)
                .values(rows)
                .on_conflict_do_nothing(index_elements=["source_id", "external_id"])
                .returning(NewsItem.__table__.c.id)  # type: ignore[attr-defined]
            )
            result = await db.execute(stmt)
            inserted_ids = list(result.scalars().all())
            inserted_count = len(inserted_ids)
            conflict_skipped = len(rows) - inserted_count

        await db.execute(
            update(NewsSource)
            .where(NewsSource.id == source_id)  # type: ignore[arg-type]
            .values(last_fetched_at=fetched_at, updated_at=fetched_at)
        )
        await db.commit()

        return inserted_count, conflict_skipped

    try:
        return await _attempt()
    except Exception as exc:
        await db.rollback()
        if _is_transient_db_error(exc):
            logger.warning("Transient DB error during news ingest; retrying once")
            return await _attempt()
        raise


async def fetch_rss_feed(url: str) -> list[dict]:
    """Parse RSS feed and extract normalized fields.

    Uses feedparser library to handle various RSS/Atom formats.
    Extracts and normalizes fields according to the mapping:
    - <title> -> title
    - <description> -> description
    - <link> -> url
    - <guid> -> external_id
    - <pubDate> -> published_at
    - <dc:creator> -> author
    - <enclosure url="..."> -> image_url

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

    # feedparser is synchronous; run it off the event loop to avoid blocking.
    feed = await asyncio.to_thread(feedparser.parse, content)

    if feed.bozo:
        logger.warning(f"Feed parse warning for {url}: {feed.bozo_exception}")

    entries: list[dict] = []
    for entry in feed.entries:
        # Extract image URL from enclosure or media content
        image_url = _extract_image_url(entry)

        # Parse published date
        published_at = _parse_published_date(entry)

        entries.append(
            {
                "title": entry.get("title", ""),
                "description": _clean_description(entry.get("description", "")),
                "link": entry.get("link", ""),
                "guid": entry.get("id", entry.get("link", "")),
                "author": entry.get("author", entry.get("dc_creator", "")),
                "image_url": image_url,
                "published_at": published_at,
            }
        )

    return entries


def _extract_image_url(entry: feedparser.FeedParserDict) -> Optional[str]:
    """Extract image URL from RSS entry.

    Checks enclosures and media content for image URLs.

    Args:
        entry: feedparser entry dict

    Returns:
        Image URL if found, None otherwise
    """
    # Check enclosures first
    enclosures = entry.get("enclosures", [])
    for enclosure in enclosures:
        enc_type = enclosure.get("type", "")
        if enc_type.startswith("image/"):
            return enclosure.get("url")

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


def _parse_published_date(entry: feedparser.FeedParserDict) -> datetime:
    """Parse published date from RSS entry.

    Tries multiple date fields and formats. Returns naive UTC datetime.

    Args:
        entry: feedparser entry dict

    Returns:
        Parsed datetime (naive UTC) or current time if parsing fails
    """
    # feedparser usually provides parsed time tuple
    if hasattr(entry, "published_parsed") and entry.published_parsed:
        try:
            # Create naive datetime from time tuple
            return datetime(*entry.published_parsed[:6])
        except Exception:
            pass

    # Try parsing the raw string
    published = entry.get("published", entry.get("pubDate", ""))
    if published:
        try:
            dt = parsedate_to_datetime(published)
            # Convert to naive UTC
            return dt.replace(tzinfo=None)
        except Exception:
            pass

    # Fallback to current time (naive UTC)
    return datetime.utcnow()


def _clean_description(description: str) -> str:
    """Clean HTML from description text.

    Simple HTML tag stripping. For more complex cleaning,
    consider using BeautifulSoup.

    Args:
        description: Raw description with possible HTML

    Returns:
        Cleaned text
    """
    if not description:
        return ""

    # Simple approach: strip common HTML tags
    # For production, consider BeautifulSoup for proper HTML handling
    import re

    # Remove HTML tags
    text = re.sub(r"<[^>]+>", "", description)
    # Normalize whitespace
    text = re.sub(r"\s+", " ", text)
    # Decode common HTML entities
    text = text.replace("&amp;", "&")
    text = text.replace("&lt;", "<")
    text = text.replace("&gt;", ">")
    text = text.replace("&quot;", '"')
    text = text.replace("&#39;", "'")
    text = text.replace("&nbsp;", " ")

    return text.strip()
