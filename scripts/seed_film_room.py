#!/usr/bin/env python
"""Seed YouTube channels for the film room.

Usage:
    conda run -n draftguru python scripts/seed_film_room.py

Resolves YouTube handles via the API, verifies recent uploads,
inserts channel rows, then optionally runs video ingestion.
"""

import asyncio
import logging
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from dotenv import load_dotenv
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=10.0)


@dataclass
class ChannelCandidate:
    """A candidate channel to seed."""

    handle: str
    display_name: str
    is_draft_focused: bool


# Primary candidates grouped by category
CANDIDATES: list[ChannelCandidate] = [
    # Scouting Reports (is_draft_focused=True)
    ChannelCandidate("@HoopIntellect", "Hoop Intellect", True),
    ChannelCandidate("@nocaborern", "No Ceilings", True),
    ChannelCandidate("@BabcockHoops", "Babcock Hoops", True),
    ChannelCandidate("@DraftDeeper", "Draft Deeper", True),
    # Conversations (is_draft_focused=True)
    ChannelCandidate("@LockedOnNBADraft", "Locked On NBA Draft", True),
    ChannelCandidate("@GameTheoryPod", "Game Theory Podcast", True),
    ChannelCandidate("@TheRingerNBA", "The Ringer NBA", True),
    # Think Pieces (is_draft_focused=False)
    ChannelCandidate("@ThinkingBasketball", "Thinking Basketball", False),
    ChannelCandidate("@baborern", "BBallBreakdown", False),
    ChannelCandidate("@AndyHoops", "Andy Hoops", False),
    # Highlights & Montage (is_draft_focused=False)
    ChannelCandidate("@ballislife", "Ballislife", False),
    ChannelCandidate("@overtime", "Overtime", False),
    ChannelCandidate("@MaxPreps", "MaxPreps", False),
    ChannelCandidate("@PrepHoops", "Prep Hoops", False),
]

# Fallback candidates if primaries don't resolve
FALLBACK_CANDIDATES: list[ChannelCandidate] = [
    ChannelCandidate("@NoCeilingsNBA", "No Ceilings", True),
    ChannelCandidate("@SwishCultures", "Swish Cultures", False),
    ChannelCandidate("@HoopsProspects", "Hoops Prospects", True),
    ChannelCandidate("@NBADraftRoom", "NBA Draft Room", True),
    ChannelCandidate("@RafaelBarlowe", "Rafael Barlowe", True),
]


@dataclass
class ResolvedChannel:
    """Channel metadata resolved from the YouTube API."""

    channel_id: str
    name: str
    display_name: str
    description: str
    thumbnail_url: str | None
    uploads_playlist_id: str | None
    channel_url: str
    is_draft_focused: bool


async def resolve_channel(
    handle: str,
    display_name: str,
    is_draft_focused: bool,
    api_key: str,
) -> ResolvedChannel | None:
    """Resolve a YouTube handle to channel metadata via the API."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.get(
                "https://www.googleapis.com/youtube/v3/channels",
                params={
                    "part": "snippet,contentDetails",
                    "forHandle": handle,
                    "key": api_key,
                },
            )
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        logger.warning(f"  API error for {handle}: {exc}")
        return None

    items = data.get("items", [])
    if not items:
        logger.warning(f"  MISS: {handle} — no channel found")
        return None

    item = items[0]
    snippet = item.get("snippet", {})
    content_details = item.get("contentDetails", {})
    channel_id = item.get("id", "")

    thumbnails = snippet.get("thumbnails", {})
    thumbnail_url = None
    for key in ("medium", "high", "default"):
        entry = thumbnails.get(key)
        if isinstance(entry, dict) and entry.get("url"):
            thumbnail_url = entry["url"]
            break

    uploads_playlist_id = content_details.get("relatedPlaylists", {}).get("uploads")

    return ResolvedChannel(
        channel_id=channel_id,
        name=snippet.get("title", display_name),
        display_name=display_name,
        description=(snippet.get("description") or "")[:500],
        thumbnail_url=thumbnail_url,
        uploads_playlist_id=uploads_playlist_id,
        channel_url=f"https://www.youtube.com/channel/{channel_id}",
        is_draft_focused=is_draft_focused,
    )


async def verify_recent_uploads(
    uploads_playlist_id: str,
    api_key: str,
    max_age_days: int = 30,
) -> bool:
    """Check if the channel has uploads within the last max_age_days."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.get(
                "https://www.googleapis.com/youtube/v3/playlistItems",
                params={
                    "part": "contentDetails",
                    "playlistId": uploads_playlist_id,
                    "maxResults": 1,
                    "key": api_key,
                },
            )
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        logger.warning(f"  Could not check recency: {exc}")
        return True  # Assume active on API error

    items = data.get("items", [])
    if not items:
        return False

    published_str = items[0].get("contentDetails", {}).get("videoPublishedAt")
    if not published_str:
        return True

    try:
        published = datetime.fromisoformat(
            published_str.replace("Z", "+00:00")
        ).astimezone(UTC)
        cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
        return published >= cutoff
    except ValueError:
        return True


async def seed_channels() -> None:
    """Resolve, verify, and insert YouTube channels."""
    import os

    from app.schemas.youtube_channels import YouTubeChannel

    api_key = os.getenv("YOUTUBE_API_KEY")
    if not api_key:
        print("ERROR: YOUTUBE_API_KEY not configured in .env")
        sys.exit(1)

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL not configured in .env")
        sys.exit(1)

    engine = create_async_engine(database_url, echo=False)
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    resolved: list[ResolvedChannel] = []
    failed: list[str] = []

    # Phase 1: Resolve primary candidates
    print("\n=== Resolving YouTube channels ===\n")
    for candidate in CANDIDATES:
        print(f"Resolving {candidate.handle} ({candidate.display_name})...")
        channel = await resolve_channel(
            candidate.handle,
            candidate.display_name,
            candidate.is_draft_focused,
            api_key,
        )
        if channel:
            # Verify recency
            if channel.uploads_playlist_id:
                is_recent = await verify_recent_uploads(
                    channel.uploads_playlist_id, api_key
                )
                if not is_recent:
                    print(f"  STALE: {candidate.handle} — no uploads in 30 days")
                    failed.append(candidate.handle)
                    continue
            print(f"  OK: {channel.name} ({channel.channel_id})")
            resolved.append(channel)
        else:
            failed.append(candidate.handle)

    # Phase 2: Try fallbacks for any failures
    if failed and len(resolved) < 15:
        print(f"\n=== Trying fallback channels ({len(failed)} failures) ===\n")
        for candidate in FALLBACK_CANDIDATES:
            if len(resolved) >= 18:
                break
            # Skip if we already have a channel with the same display_name
            existing_names = {c.display_name for c in resolved}
            if candidate.display_name in existing_names:
                continue

            print(f"Resolving {candidate.handle} ({candidate.display_name})...")
            channel = await resolve_channel(
                candidate.handle,
                candidate.display_name,
                candidate.is_draft_focused,
                api_key,
            )
            if channel:
                if channel.uploads_playlist_id:
                    is_recent = await verify_recent_uploads(
                        channel.uploads_playlist_id, api_key
                    )
                    if not is_recent:
                        print(f"  STALE: {candidate.handle}")
                        continue
                print(f"  OK: {channel.name} ({channel.channel_id})")
                resolved.append(channel)

    # Phase 3: Insert into database
    print(f"\n=== Inserting {len(resolved)} channels into database ===\n")

    async with session_factory() as session:
        added = 0
        skipped = 0

        for ch in resolved:
            stmt = select(YouTubeChannel).where(
                YouTubeChannel.channel_id == ch.channel_id  # type: ignore[arg-type]
            )
            result = await session.execute(stmt)
            existing = result.scalar_one_or_none()

            if existing:
                print(f"  SKIP: {ch.display_name} (already exists)")
                skipped += 1
                continue

            now = datetime.now(UTC).replace(tzinfo=None)
            channel_row = YouTubeChannel(
                name=ch.name,
                display_name=ch.display_name,
                channel_id=ch.channel_id,
                channel_url=ch.channel_url,
                thumbnail_url=ch.thumbnail_url,
                description=ch.description,
                uploads_playlist_id=ch.uploads_playlist_id,
                is_draft_focused=ch.is_draft_focused,
                is_active=True,
                fetch_interval_minutes=60,
                created_at=now,
                updated_at=now,
            )
            session.add(channel_row)
            print(f"  ADD: {ch.display_name} ({ch.channel_id})")
            added += 1

        await session.commit()

    print(f"\nSeeding complete: {added} added, {skipped} skipped, {len(failed)} failed")

    if added == 0 and skipped > 0:
        print("All channels already exist. Skipping ingestion prompt.")
        await engine.dispose()
        return

    # Phase 4: Run ingestion
    print("\n=== Running video ingestion ===\n")
    if "--no-ingest" in sys.argv:
        print("Skipping ingestion (--no-ingest flag).")
        await engine.dispose()
        return

    try:
        answer = input("Run ingestion cycle now? [Y/n]: ").strip().lower()
    except EOFError:
        answer = "y"  # default to yes in non-interactive mode

    if answer in ("", "y", "yes"):
        from app.services.video_ingestion_service import run_ingestion_cycle

        async with session_factory() as session:
            ingestion_result = await run_ingestion_cycle(session)

        print("\nIngestion complete:")
        print(f"  Channels processed: {ingestion_result.channels_processed}")
        print(f"  Videos added: {ingestion_result.videos_added}")
        print(f"  Videos skipped: {ingestion_result.videos_skipped}")
        print(f"  Videos filtered: {ingestion_result.videos_filtered}")
        print(f"  Mentions added: {ingestion_result.mentions_added}")
        if ingestion_result.errors:
            print("  Errors:")
            for error in ingestion_result.errors:
                print(f"    - {error}")
    else:
        print("Skipping ingestion. Run manually via admin or API.")

    await engine.dispose()


if __name__ == "__main__":
    print("=== Film Room Channel Seeder ===")
    asyncio.run(seed_channels())
