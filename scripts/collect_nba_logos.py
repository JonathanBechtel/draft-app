#!/usr/bin/env python
"""Download, process, and upload NBA team logos.

Fetches logos from ESPN CDN, resizes to a consistent 200x200 PNG with
transparent background, and uploads via the app's S3 client (which
falls back to local filesystem in dev mode).

Usage:
    python scripts/collect_nba_logos.py              # Process all teams
    python scripts/collect_nba_logos.py --dry-run     # Download + process only
    python scripts/collect_nba_logos.py --team LAL    # Single team
    python scripts/collect_nba_logos.py --local-only  # Force local storage
"""

import argparse
import asyncio
import io
import logging
import os
import sys
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv
from PIL import Image
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

# ESPN CDN serves 500px PNGs for every NBA team by lowercase abbreviation
ESPN_LOGO_URL = "https://a.espncdn.com/i/teamlogos/nba/500/{abbr}.png"

# ESPN uses non-standard slugs for some teams
ESPN_ABBR_OVERRIDES: dict[str, str] = {
    "UTA": "utah",
    "NOP": "no",
}

# Target dimensions for processed logos
LOGO_SIZE = (200, 200)


def process_logo(raw_bytes: bytes) -> bytes:
    """Resize and normalize a logo image to a consistent PNG.

    Args:
        raw_bytes: Raw image bytes from download.

    Returns:
        Processed PNG bytes (200x200, RGBA, optimized).
    """
    img = Image.open(io.BytesIO(raw_bytes))
    img = img.convert("RGBA")

    # Use LANCZOS resampling for high-quality downscale
    img.thumbnail(LOGO_SIZE, Image.LANCZOS)

    # Center on a transparent canvas if thumbnail isn't exactly square
    if img.size != LOGO_SIZE:
        canvas = Image.new("RGBA", LOGO_SIZE, (0, 0, 0, 0))
        offset = ((LOGO_SIZE[0] - img.width) // 2, (LOGO_SIZE[1] - img.height) // 2)
        canvas.paste(img, offset, mask=img)
        img = canvas

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


async def collect_logos(
    *,
    dry_run: bool = False,
    local_only: bool = False,
    team_filter: str | None = None,
) -> None:
    """Download, process, and upload NBA team logos.

    Args:
        dry_run: If True, download and process but don't upload or update DB.
        local_only: If True, force local filesystem storage.
        team_filter: If set, only process this abbreviation (e.g. 'LAL').
    """
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        logger.error("DATABASE_URL not set")
        sys.exit(1)

    engine = create_async_engine(db_url)
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    from app.schemas.nba_teams import NbaTeam

    # Optionally force local storage
    if local_only:
        from app.config import settings

        settings.image_storage_local = True

    from app.services.s3_client import s3_client

    # Load teams from DB
    async with session_factory() as session:
        query = select(NbaTeam)
        if team_filter:
            query = query.where(NbaTeam.abbreviation == team_filter.upper())
        result = await session.execute(query)
        teams = result.scalars().all()

    if not teams:
        logger.error("No teams found. Run seed_nba_teams.py first.")
        sys.exit(1)

    logger.info(f"Processing {len(teams)} team(s)...\n")

    succeeded = 0
    failed = 0

    async with httpx.AsyncClient(timeout=30.0) as http:
        for team in teams:
            espn_abbr = ESPN_ABBR_OVERRIDES.get(
                team.abbreviation, team.abbreviation.lower()
            )
            url = ESPN_LOGO_URL.format(abbr=espn_abbr)
            s3_key = f"logos/nba/{team.slug}.png"

            # Download
            try:
                resp = await http.get(url)
                resp.raise_for_status()
                raw_bytes = resp.content
                logger.info(
                    f"  DOWNLOAD  {team.abbreviation} — {len(raw_bytes):,} bytes"
                )
            except httpx.HTTPError as e:
                logger.error(f"  FAIL      {team.abbreviation} — download error: {e}")
                failed += 1
                continue

            # Process
            try:
                processed = process_logo(raw_bytes)
                logger.info(
                    f"  PROCESS   {team.abbreviation} — "
                    f"{len(raw_bytes):,} → {len(processed):,} bytes"
                )
            except Exception as e:
                logger.error(f"  FAIL      {team.abbreviation} — processing error: {e}")
                failed += 1
                continue

            if dry_run:
                logger.info(f"  DRY-RUN   {team.abbreviation} — skipping upload")
                succeeded += 1
                continue

            # Upload
            try:
                public_url = s3_client.upload(
                    key=s3_key,
                    data=processed,
                    content_type="image/png",
                )
                logger.info(f"  UPLOAD    {team.abbreviation} → {public_url}")
            except Exception as e:
                logger.error(f"  FAIL      {team.abbreviation} — upload error: {e}")
                failed += 1
                continue

            # Update DB
            async with session_factory() as session:
                await session.execute(
                    update(NbaTeam)
                    .where(NbaTeam.id == team.id)
                    .values(
                        logo_url=public_url,
                        updated_at=datetime.now(timezone.utc).replace(tzinfo=None),
                    )
                )
                await session.commit()

            succeeded += 1

    await engine.dispose()

    logger.info(f"\nDone: {succeeded} succeeded, {failed} failed ({len(teams)} total)")
    if dry_run:
        logger.info("(dry-run mode — nothing was uploaded or saved)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect NBA team logos")
    parser.add_argument(
        "--dry-run", action="store_true", help="Download + process only"
    )
    parser.add_argument(
        "--local-only", action="store_true", help="Force local filesystem"
    )
    parser.add_argument(
        "--team", type=str, default=None, help="Single team abbreviation"
    )
    args = parser.parse_args()

    asyncio.run(
        collect_logos(
            dry_run=args.dry_run,
            local_only=args.local_only,
            team_filter=args.team,
        )
    )


if __name__ == "__main__":
    main()
