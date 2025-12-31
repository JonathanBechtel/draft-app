#!/usr/bin/env python
"""Seed initial news sources for the news feed.

Usage:
    python scripts/seed_news_sources.py

This script adds the initial RSS sources if they don't already exist.
"""

import asyncio
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

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
    # Add more Substack feeds as identified:
    # {
    #     "name": "Another Draft Substack",
    #     "display_name": "Another Draft Substack",
    #     "feed_type": "rss",
    #     "feed_url": "https://anotherdraft.substack.com/feed",
    #     "fetch_interval_minutes": 30,
    # },
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

            # Create new source
            source = NewsSource(
                name=source_data["name"],
                display_name=source_data["display_name"],
                feed_type=FeedType(source_data["feed_type"]),
                feed_url=source_data["feed_url"],
                fetch_interval_minutes=source_data["fetch_interval_minutes"],
                is_active=True,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
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
