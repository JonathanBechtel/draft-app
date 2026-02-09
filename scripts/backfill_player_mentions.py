"""Backfill player mentions for existing news items using string matching.

Scans title + description of each NewsItem against known PlayerMaster
display names and PlayerAlias full names (case-insensitive word-boundary match).
Also creates mention rows for any existing NewsItem.player_id associations.

Usage:
    python scripts/backfill_player_mentions.py

Requires DATABASE_URL in environment or .env file.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys

from dotenv import load_dotenv

# Ensure project root is on sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

load_dotenv()

from sqlalchemy import select  # noqa: E402
from sqlalchemy.dialects.postgresql import insert  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from app.schemas.news_item_player_mentions import (  # noqa: E402
    MentionSource,
    NewsItemPlayerMention,
)
from app.schemas.news_items import NewsItem  # noqa: E402
from app.schemas.player_aliases import PlayerAlias  # noqa: E402
from app.schemas.players_master import PlayerMaster  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


async def _build_name_lookup(
    db: AsyncSession,
) -> dict[re.Pattern[str], int]:
    """Build a word-boundary regex -> player_id lookup from display names and aliases."""
    lookup: dict[re.Pattern[str], int] = {}

    # Load display names
    result = await db.execute(
        select(PlayerMaster.id, PlayerMaster.display_name).where(
            PlayerMaster.display_name.isnot(None)  # type: ignore[union-attr]
        )
    )
    for player_id, display_name in result.all():
        if display_name and len(display_name) >= 4:
            pattern = re.compile(r"\b" + re.escape(display_name.lower()) + r"\b")
            lookup[pattern] = player_id  # type: ignore[assignment]

    # Load aliases
    result = await db.execute(select(PlayerAlias.player_id, PlayerAlias.full_name))
    for player_id, full_name in result.all():
        if full_name and len(full_name) >= 4:
            pattern = re.compile(r"\b" + re.escape(full_name.lower()) + r"\b")
            lookup[pattern] = player_id  # type: ignore[assignment]

    return lookup


def _find_mentions(text: str, name_lookup: dict[re.Pattern[str], int]) -> set[int]:
    """Find all player IDs mentioned in the given text via word-boundary regex."""
    text_lower = text.lower()
    found: set[int] = set()
    for pattern, player_id in name_lookup.items():
        if pattern.search(text_lower):
            found.add(player_id)
    return found


async def backfill() -> None:
    """Run the backfill process."""
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        logger.error("DATABASE_URL not set")
        sys.exit(1)

    engine = create_async_engine(database_url, echo=False)

    session_factory = async_sessionmaker(
        bind=engine, expire_on_commit=False, class_=AsyncSession
    )

    async with session_factory() as db:
        name_lookup = await _build_name_lookup(db)
        logger.info(f"Loaded {len(name_lookup)} player name entries for matching")

        # Fetch all news items
        result = await db.execute(
            select(
                NewsItem.id,
                NewsItem.title,
                NewsItem.description,
                NewsItem.player_id,
            )
        )
        items = result.all()
        logger.info(f"Processing {len(items)} news items")

        mention_rows: list[dict] = []
        seen: set[tuple[int, int]] = set()

        for news_item_id, title, description, existing_player_id in items:
            # Word-boundary regex matching
            text = f"{title or ''} {description or ''}"
            matched_ids = _find_mentions(text, name_lookup)

            # Also include existing player_id association
            if existing_player_id is not None:
                matched_ids.add(existing_player_id)

            for player_id in matched_ids:
                key = (news_item_id, player_id)
                if key not in seen:
                    seen.add(key)
                    mention_rows.append(
                        {
                            "news_item_id": news_item_id,
                            "player_id": player_id,
                            "source": MentionSource.BACKFILL,
                        }
                    )

        if mention_rows:
            stmt = (
                insert(NewsItemPlayerMention)
                .values(mention_rows)
                .on_conflict_do_nothing(constraint="uq_news_item_player_mention")
                .returning(NewsItemPlayerMention.__table__.c.id)  # type: ignore[attr-defined]
            )
            result = await db.execute(stmt)
            inserted_count = len(list(result.scalars().all()))
            await db.commit()
            logger.info(
                f"Inserted {inserted_count} mention rows "
                f"({len(mention_rows)} attempted, "
                f"{len(mention_rows) - inserted_count} already existed)"
            )
        else:
            logger.info("No mentions found to backfill")

    await engine.dispose()
    logger.info("Backfill complete")


if __name__ == "__main__":
    asyncio.run(backfill())
