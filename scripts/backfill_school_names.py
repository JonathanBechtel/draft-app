#!/usr/bin/env python
"""Canonicalize school names in players_master using the reviewed mapping.

Reads school_mapping.json and updates players_master.school to canonical
values, using school_raw (the preserved original) as the match key.

Usage:
    python scripts/backfill_school_names.py             # apply changes
    python scripts/backfill_school_names.py --dry-run   # preview only
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

load_dotenv()

MAPPING_PATH = Path(__file__).parent / "data" / "school_mapping.json"


async def backfill_school_names(*, dry_run: bool = False) -> None:
    """Update players_master.school to canonical names."""
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set")
        sys.exit(1)

    if not MAPPING_PATH.exists():
        print(f"ERROR: {MAPPING_PATH} not found.")
        sys.exit(1)

    with open(MAPPING_PATH, encoding="utf-8") as f:
        mapping: dict[str, str | None] = json.load(f)

    # Only process entries where raw != canonical (and canonical is not null)
    updates = {
        raw: canonical
        for raw, canonical in mapping.items()
        if canonical is not None and raw != canonical
    }

    print(f"Mapping entries: {len(mapping)} total, {len(updates)} to update")

    engine = create_async_engine(db_url)
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    total_players = 0
    total_schools = 0

    async with session_factory() as session:
        for raw, canonical in sorted(updates.items()):
            result = await session.execute(
                text(
                    "SELECT COUNT(*) FROM players_master "
                    "WHERE school_raw = :raw AND school != :canonical"
                ),
                {"raw": raw, "canonical": canonical},
            )
            count = result.scalar() or 0

            if count == 0:
                continue

            total_schools += 1
            total_players += count

            if dry_run:
                print(f"  WOULD UPDATE  {count:3d} players: {raw} → {canonical}")
            else:
                await session.execute(
                    text(
                        "UPDATE players_master SET school = :canonical "
                        "WHERE school_raw = :raw"
                    ),
                    {"raw": raw, "canonical": canonical},
                )
                print(f"  UPDATE  {count:3d} players: {raw} → {canonical}")

        if not dry_run:
            await session.commit()

    await engine.dispose()

    if dry_run:
        print(
            f"\nDry run: {total_players} players across {total_schools} schools would be updated"
        )
    else:
        print(f"\nDone: {total_players} players updated across {total_schools} schools")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill canonical school names")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes only")
    args = parser.parse_args()
    asyncio.run(backfill_school_names(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
