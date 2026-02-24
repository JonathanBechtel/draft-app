#!/usr/bin/env python
"""Seed initial news sources for the news feed.

Usage:
    python scripts/seed_news_sources.py

This script adds the initial RSS sources if they don't already exist.
"""

import asyncio
import sys
from datetime import UTC, datetime

from dotenv import load_dotenv
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

load_dotenv()


# Initial sources to seed
INITIAL_SOURCES = [
    {
        "name": "Floor and Ceiling",
        "display_name": "Floor and Ceiling",
        "feed_type": "rss",
        "feed_url": "https://floorandceiling.substack.com/feed",
        "fetch_interval_minutes": 30,
    },
    {
        "name": "No Ceilings",
        "display_name": "No Ceilings",
        "feed_type": "rss",
        "feed_url": "https://www.noceilingsnba.com/feed",
        "fetch_interval_minutes": 30,
    },
    {
        "name": "NBA Big Board",
        "display_name": "NBA Big Board",
        "feed_type": "rss",
        "feed_url": "https://www.nbabigboard.com/feed",
        "fetch_interval_minutes": 30,
    },
    {
        "name": "The Box And One",
        "display_name": "The Box And One",
        "feed_type": "rss",
        "feed_url": "https://theboxandone.substack.com/feed",
        "fetch_interval_minutes": 30,
    },
    {
        "name": "Draft Stack",
        "display_name": "Draft Stack",
        "feed_type": "rss",
        "feed_url": "https://draftstack.substack.com/feed",
        "fetch_interval_minutes": 30,
    },
    {
        "name": "Ersin Demir",
        "display_name": "Ersin Demir",
        "feed_type": "rss",
        "feed_url": "https://edemirnba.substack.com/feed",
        "fetch_interval_minutes": 30,
    },
    {
        "name": "Assisted Development",
        "display_name": "Assisted Development",
        "feed_type": "rss",
        "feed_url": "https://assisteddevelopment.substack.com/feed",
        "fetch_interval_minutes": 30,
    },
    {
        "name": "NBA Draft Room",
        "display_name": "NBA Draft Room",
        "feed_type": "rss",
        "feed_url": "https://nbadraftroom.com/feed/",
        "fetch_interval_minutes": 30,
    },
]


async def seed_sources() -> None:
    """Seed news sources into the database."""
    import os

    from app.schemas.news_sources import FeedType, NewsSource

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL not configured")
        sys.exit(1)

    # Create async engine
    engine = create_async_engine(database_url, echo=False)
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    async with session_factory() as session:
        added = 0
        skipped = 0

        for source_data in INITIAL_SOURCES:
            # Check if source already exists
            stmt = select(NewsSource).where(
                NewsSource.feed_url == source_data["feed_url"]
            )
            result = await session.execute(stmt)
            existing = result.scalar_one_or_none()

            if existing:
                print(f"  SKIP: {source_data['name']} (already exists)")
                skipped += 1
                continue

            # Create new source (use naive UTC datetimes to match schema)
            source = NewsSource(
                name=source_data["name"],
                display_name=source_data["display_name"],
                feed_type=FeedType(source_data["feed_type"]),
                feed_url=source_data["feed_url"],
                fetch_interval_minutes=source_data["fetch_interval_minutes"],
                is_active=True,
                created_at=datetime.now(UTC).replace(tzinfo=None),
                updated_at=datetime.now(UTC).replace(tzinfo=None),
            )
            session.add(source)
            print(f"  ADD: {source_data['name']}")
            added += 1

        await session.commit()
        print(f"\nSeeding complete: {added} added, {skipped} skipped")

    await engine.dispose()


if __name__ == "__main__":
    print("Seeding news sources...")
    asyncio.run(seed_sources())
