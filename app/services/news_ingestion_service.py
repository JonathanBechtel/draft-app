"""News feed ingestion service.

Handles fetching, parsing, and storing news from various feed types.
Currently supports RSS feeds with architecture for future expansion.
"""

import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

import feedparser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.news import IngestionResult
from app.schemas.news_items import NewsItem
from app.schemas.news_sources import FeedType, NewsSource
from app.services.news_service import get_active_sources
from app.services.news_summarization_service import news_summarization_service

logger = logging.getLogger(__name__)


async def run_ingestion_cycle(db: AsyncSession) -> IngestionResult:
    """Process all active sources based on their feed type.

    Iterates through all active sources and dispatches to the
    appropriate ingestion handler based on feed_type.

    Args:
        db: Async database session

    Returns:
        IngestionResult with counts and any errors
    """
    sources = await get_active_sources(db)

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

    return IngestionResult(
        sources_processed=len(sources),
        items_added=total_added,
        items_skipped=total_skipped,
        errors=errors,
    )


async def ingest_rss_source(
    db: AsyncSession,
    source: NewsSource,
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
    logger.info(f"Ingesting RSS feed: {source.name} ({source.feed_url})")

    entries = await fetch_rss_feed(source.feed_url)
    logger.info(f"Fetched {len(entries)} entries from {source.name}")

    items_added = 0
    items_skipped = 0

    source_id = source.id
    if source_id is None:
        raise ValueError("Source ID is required")

    for entry in entries:
        external_id = entry.get("guid", entry.get("link", ""))
        if not external_id:
            logger.warning(
                f"Skipping entry without ID: {entry.get('title', 'unknown')}"
            )
            items_skipped += 1
            continue

        # Check for existing item (deduplication)
        existing = await db.execute(
            select(NewsItem.id).where(  # type: ignore[call-overload]
                NewsItem.source_id == source_id,
                NewsItem.external_id == external_id,
            )
        )
        if existing.scalar_one_or_none() is not None:
            logger.debug(f"Skipping duplicate: {external_id}")
            items_skipped += 1
            continue

        # Extract fields from RSS entry
        title = entry.get("title", "Untitled")
        description = entry.get("description", "")
        url = entry.get("link", "")
        image_url = entry.get("image_url")
        author = entry.get("author")
        published_at = entry.get("published_at", datetime.now(timezone.utc))

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

            tag = NewsItemTag.ANALYSIS

        # Create news item
        news_item = NewsItem(
            source_id=source_id,
            external_id=external_id,
            title=title,
            description=description,
            url=url,
            image_url=image_url,
            author=author,
            summary=summary,
            tag=tag,
            published_at=published_at,
        )
        db.add(news_item)
        items_added += 1
        logger.debug(f"Added: {title[:50]}")

    # Update source's last_fetched_at
    source.last_fetched_at = datetime.now(timezone.utc)
    source.updated_at = datetime.now(timezone.utc)

    await db.commit()
    logger.info(f"Ingested {source.name}: {items_added} added, {items_skipped} skipped")

    return items_added, items_skipped


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
    # feedparser is synchronous, but fast enough for our needs
    feed = feedparser.parse(url)

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

    Tries multiple date fields and formats.

    Args:
        entry: feedparser entry dict

    Returns:
        Parsed datetime (UTC) or current time if parsing fails
    """
    # feedparser usually provides parsed time tuple
    if hasattr(entry, "published_parsed") and entry.published_parsed:
        try:
            # Create datetime from time tuple and add timezone
            dt = datetime(*entry.published_parsed[:6])
            return dt.replace(tzinfo=timezone.utc)
        except Exception:
            pass

    # Try parsing the raw string
    published = entry.get("published", entry.get("pubDate", ""))
    if published:
        try:
            return parsedate_to_datetime(published)
        except Exception:
            pass

    # Fallback to current time
    return datetime.now(timezone.utc)


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
