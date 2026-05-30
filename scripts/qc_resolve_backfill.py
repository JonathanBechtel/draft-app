#!/usr/bin/env python
"""One-off QC resolution pass for the historical backfill (dev).

Applies an EXPLICIT, auditable decision map to the unresolved entries on the
backfilled boards (id>=10), honoring the entity-resolution philosophy:
  * ASSIGN only same-name normalization matches to a single canonical record.
  * MINT a stub for clearly-new prospects (minted once per name, then assigned
    to that name's other entries).
  * LEAVE everything else UNRESOLVED — dup-entangled names (fix via merge, not a
    guess), bare common surnames, and single-occurrence/uncertain names.

Boards must be PENDING (the service edit-gate). Prints a full action log.
"""

from __future__ import annotations

import asyncio
import os
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# raw_name -> canonical players_master.id (same-name normalization; single record)
ASSIGN: dict[str, int] = {
    "AJ Dybansta": 5390,
    "KJ Lewis": 6070,
    "Kyian Anthony": 5607,
    "Ruben Chinyelu": 5745,
    "Zuby Ejifor": 5528,
    "Amaël L’Etang": 5568,
    "Nik Khamenia": 5716,
    "Ugonna Kingsley Onyenso": 5526,
    "Isaih Harwell": 5554,
    "Isiah “Zai” Harwell": 5554,
    "Andrej Stoakavic": 5632,
    "Stojakovic": 5632,
    "Chris Cenac, Jr.": 5387,
    "Darius Acuff, Jr.": 5384,
    "Acuff, Jr.": 5384,
    "Christian Anderson, Jr.": 5382,
    "Mikel Brown, Jr.": 5389,
    "Morez Johnson, Jr.": 5529,
    "Labaron Philon, Jr.": 1709,
    "Tarris Reed, Jr.": 5483,
    "Taris Reed Jr.": 5483,
    "Zvonimir Ivisic": 5738,
    "Billy Richmond III": 5414,
}

# raw_names to mint as new stub prospects (corroborated on >=2 boards)
MINT: set[str] = {
    "Joson Sanon",
    "Jaland Lowe",
    "JJ Starling",
    "Kam Craft",
    "Moustapha Thiam",
    "Vyctorius Miller",
    "Kerr Kriisa",
    "Roman Siulepa",
    "Youssef Khayat",
    "Skyy Clark",
    "Bassala Bagayoko",
}


def _load_all_schemas() -> None:
    import importlib
    import pkgutil

    import app.schemas as schemas_pkg

    for mod in pkgutil.iter_modules(schemas_pkg.__path__):
        importlib.import_module(f"app.schemas.{mod.name}")


async def main() -> None:
    _load_all_schemas()
    from app.schemas.boards import BoardEntry
    from app.services import board_service as svc

    engine = create_async_engine(
        os.environ["DATABASE_URL"], echo=False, pool_pre_ping=True
    )
    sf = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    tally = {"assigned": 0, "minted": 0, "mint_assigned": 0, "skipped": 0}
    minted_ids: dict[str, int] = {}
    try:
        async with sf() as db:
            rows = (
                await db.execute(
                    select(BoardEntry.id, BoardEntry.raw_name)  # type: ignore[call-overload]
                    .where(BoardEntry.player_id.is_(None))  # type: ignore[union-attr]
                    .where(BoardEntry.board_id >= 10)  # type: ignore[arg-type]
                    .order_by(BoardEntry.board_id, BoardEntry.position)
                )
            ).all()

            for entry_id, raw_name in rows:
                try:
                    if raw_name in ASSIGN:
                        await svc.assign_entry(
                            db, entry_id=entry_id, player_id=ASSIGN[raw_name]
                        )
                        await db.commit()
                        tally["assigned"] += 1
                        print(f"[assign] '{raw_name}' -> {ASSIGN[raw_name]}")
                    elif raw_name in MINT:
                        if raw_name not in minted_ids:
                            e = await svc.mint_stub_for_entry(db, entry_id=entry_id)
                            await db.commit()
                            minted_ids[raw_name] = e.player_id  # type: ignore[assignment]
                            tally["minted"] += 1
                            print(f"[mint]   '{raw_name}' -> NEW {e.player_id}")
                        else:
                            await svc.assign_entry(
                                db, entry_id=entry_id, player_id=minted_ids[raw_name]
                            )
                            await db.commit()
                            tally["mint_assigned"] += 1
                    else:
                        tally["skipped"] += 1
                except Exception as exc:  # noqa: BLE001
                    await db.rollback()
                    tally["skipped"] += 1
                    print(
                        f"[skip]   '{raw_name}' entry={entry_id}: {type(exc).__name__}: {str(exc)[:80]}"
                    )
    finally:
        await engine.dispose()

    print("\n=== tally ===")
    for k, v in tally.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    asyncio.run(main())
