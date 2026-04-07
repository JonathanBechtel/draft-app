#!/usr/bin/env python
"""Seed the college_schools table from generated seed data.

Usage:
    python scripts/seed_college_schools.py

Idempotent: skips schools that already exist (matched by name).
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

load_dotenv()

SEED_DATA_PATH = Path(__file__).parent / "data" / "college_schools.json"


async def seed_college_schools() -> None:
    """Insert college schools from seed data, skipping existing rows."""
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set")
        sys.exit(1)

    if not SEED_DATA_PATH.exists():
        print(
            f"ERROR: {SEED_DATA_PATH} not found. Run generate_school_seed_data.py first."
        )
        sys.exit(1)

    with open(SEED_DATA_PATH, encoding="utf-8") as f:
        schools = json.load(f)

    engine = create_async_engine(db_url)
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    from app.schemas.college_schools import CollegeSchool

    added = 0
    skipped = 0

    async with session_factory() as session:
        for school_data in schools:
            result = await session.execute(
                select(CollegeSchool).where(CollegeSchool.name == school_data["name"])
            )
            existing = result.scalar_one_or_none()

            if existing:
                skipped += 1
                continue

            now = datetime.now(timezone.utc).replace(tzinfo=None)
            school = CollegeSchool(
                name=school_data["name"],
                slug=school_data["slug"],
                conference=school_data.get("conference"),
                espn_id=school_data.get("espn_id"),
                logo_url=None,
                primary_color=school_data.get("primary_color"),
                secondary_color=school_data.get("secondary_color"),
                created_at=now,
                updated_at=now,
            )
            session.add(school)
            added += 1

            if added % 50 == 0:
                print(f"  Added {added} schools...")

        await session.commit()

    await engine.dispose()
    print(f"\nDone: {added} added, {skipped} skipped ({added + skipped} total)")


if __name__ == "__main__":
    asyncio.run(seed_college_schools())
