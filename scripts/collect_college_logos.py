#!/usr/bin/env python
"""Download, process, and upload college school logos.

Fetches logos from ESPN CDN using ESPN team IDs, resizes to 200x200 PNG,
and uploads via the app's S3 client.

Usage:
    python scripts/collect_college_logos.py              # all schools with ESPN IDs
    python scripts/collect_college_logos.py --dry-run     # download + process only
    python scripts/collect_college_logos.py --school duke # single school by slug
    python scripts/collect_college_logos.py --local-only  # force local storage
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

# ESPN CDN serves 500px PNGs for NCAA teams by numeric ID
ESPN_LOGO_URL = "https://a.espncdn.com/i/teamlogos/ncaa/500/{espn_id}.png"

LOGO_SIZE = (200, 200)


def process_logo(raw_bytes: bytes) -> bytes:
    """Resize and normalize a logo image to a consistent PNG."""
    img = Image.open(io.BytesIO(raw_bytes))
    img = img.convert("RGBA")
    img.thumbnail(LOGO_SIZE, Image.LANCZOS)

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
    school_filter: str | None = None,
) -> None:
    """Download, process, and upload college logos."""
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        logger.error("DATABASE_URL not set")
        sys.exit(1)

    engine = create_async_engine(db_url)
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    from app.schemas.college_schools import CollegeSchool

    if local_only:
        from app.config import settings

        settings.image_storage_local = True

    from app.services.s3_client import s3_client

    # Load schools with ESPN IDs
    async with session_factory() as session:
        query = select(CollegeSchool).where(CollegeSchool.espn_id.is_not(None))  # type: ignore[union-attr]
        if school_filter:
            query = query.where(CollegeSchool.slug == school_filter)
        result = await session.execute(query)
        schools = result.scalars().all()

    if not schools:
        logger.error(
            "No schools found with ESPN IDs. Run seed_college_schools.py first."
        )
        sys.exit(1)

    logger.info(f"Processing {len(schools)} school(s)...\n")

    succeeded = 0
    failed = 0

    async with httpx.AsyncClient(timeout=30.0) as http:
        for school in schools:
            url = ESPN_LOGO_URL.format(espn_id=school.espn_id)
            s3_key = f"logos/college/{school.slug}.png"

            # Download
            try:
                resp = await http.get(url)
                resp.raise_for_status()
                raw_bytes = resp.content
                logger.info(f"  DOWNLOAD  {school.name:30s} — {len(raw_bytes):,} bytes")
            except httpx.HTTPError as e:
                logger.error(f"  FAIL      {school.name:30s} — download error: {e}")
                failed += 1
                continue

            # Process
            try:
                processed = process_logo(raw_bytes)
            except Exception as e:
                logger.error(f"  FAIL      {school.name:30s} — processing error: {e}")
                failed += 1
                continue

            if dry_run:
                logger.info(f"  DRY-RUN   {school.name:30s} — skipping upload")
                succeeded += 1
                continue

            # Upload
            try:
                public_url = s3_client.upload(
                    key=s3_key,
                    data=processed,
                    content_type="image/png",
                )
                logger.info(f"  UPLOAD    {school.name:30s} → {public_url}")
            except Exception as e:
                logger.error(f"  FAIL      {school.name:30s} — upload error: {e}")
                failed += 1
                continue

            # Update DB
            async with session_factory() as session:
                await session.execute(
                    update(CollegeSchool)
                    .where(CollegeSchool.id == school.id)
                    .values(
                        logo_url=public_url,
                        updated_at=datetime.now(timezone.utc).replace(tzinfo=None),
                    )
                )
                await session.commit()

            succeeded += 1

    await engine.dispose()

    logger.info(
        f"\nDone: {succeeded} succeeded, {failed} failed ({len(schools)} total)"
    )
    if dry_run:
        logger.info("(dry-run mode — nothing was uploaded or saved)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect college school logos")
    parser.add_argument(
        "--dry-run", action="store_true", help="Download + process only"
    )
    parser.add_argument(
        "--local-only", action="store_true", help="Force local filesystem"
    )
    parser.add_argument("--school", type=str, default=None, help="Single school slug")
    args = parser.parse_args()

    asyncio.run(
        collect_logos(
            dry_run=args.dry_run,
            local_only=args.local_only,
            school_filter=args.school,
        )
    )


if __name__ == "__main__":
    main()
