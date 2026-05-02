#!/usr/bin/env python
"""Repair stale Locked On NBA Draft podcast URLs.

The Locked On NBA Draft RSS feed publishes a stale show URL
(https://lockedonpodcasts.com/podcasts/nba-big-board/) for both the channel
<link> and every <item><link>. That URL now 404s; the show was renamed to
"NBA Draft with No Ceilings" and lives at /podcasts/nba-draft-with-no-ceilings/.

This one-off script:
  - Updates podcast_shows.website_url for the Locked On show.
  - Rewrites podcast_episodes.episode_url for every episode that still points
    at the broken URL.

Usage:
    python scripts/fix_locked_on_podcast_urls.py             # apply changes
    python scripts/fix_locked_on_podcast_urls.py --dry-run   # preview only
"""

import argparse
import asyncio
import os
import sys

from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

load_dotenv()

BROKEN_URL = "https://lockedonpodcasts.com/podcasts/nba-big-board/"
CORRECT_URL = "https://lockedonpodcasts.com/podcasts/nba-draft-with-no-ceilings/"
SHOW_NAME = "locked-on-nba-draft"


async def fix_locked_on_urls(*, dry_run: bool = False) -> None:
    """Patch the Locked On show + episode URLs."""
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set")
        sys.exit(1)

    engine = create_async_engine(db_url)
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    async with session_factory() as session:
        show_row = (
            await session.execute(
                text(
                    "SELECT id, display_name, website_url FROM podcast_shows "
                    "WHERE name = :name"
                ),
                {"name": SHOW_NAME},
            )
        ).first()

        if show_row is None:
            print(f"ERROR: no podcast_shows row with name='{SHOW_NAME}'")
            sys.exit(1)

        show_id, display_name, current_website = show_row
        print(f"Show: id={show_id} '{display_name}'")
        print(f"  current website_url: {current_website}")

        episode_count = (
            await session.execute(
                text(
                    "SELECT COUNT(*) FROM podcast_episodes "
                    "WHERE show_id = :sid AND episode_url = :bad"
                ),
                {"sid": show_id, "bad": BROKEN_URL},
            )
        ).scalar() or 0
        print(f"  episodes with broken episode_url: {episode_count}")

        needs_show_update = current_website != CORRECT_URL

        if dry_run:
            if needs_show_update:
                print(f"  WOULD UPDATE website_url -> {CORRECT_URL}")
            else:
                print("  website_url already correct, no change")
            print(
                f"  WOULD UPDATE episode_url for {episode_count} episode(s) -> {CORRECT_URL}"
            )
            print("\nDry run: no changes applied")
            await engine.dispose()
            return

        if needs_show_update:
            await session.execute(
                text(
                    "UPDATE podcast_shows SET website_url = :url, updated_at = NOW() "
                    "WHERE id = :sid"
                ),
                {"url": CORRECT_URL, "sid": show_id},
            )
            print(f"  UPDATED website_url -> {CORRECT_URL}")
        else:
            print("  website_url already correct")

        result = await session.execute(
            text(
                "UPDATE podcast_episodes SET episode_url = :good "
                "WHERE show_id = :sid AND episode_url = :bad"
            ),
            {"good": CORRECT_URL, "sid": show_id, "bad": BROKEN_URL},
        )
        print(f"  UPDATED episode_url on {result.rowcount} episode(s)")

        await session.commit()

    await engine.dispose()
    print("\nDone")


def main() -> None:
    parser = argparse.ArgumentParser(description="Fix stale Locked On podcast URLs")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes only")
    args = parser.parse_args()
    asyncio.run(fix_locked_on_urls(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
