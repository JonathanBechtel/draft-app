#!/usr/bin/env python
"""QC round 2 (dev): assign the unambiguous bare-surname leftovers.

Each name here is a distinctive surname with exactly one clear 2026 prospect
and no duplicate-record entanglement, so the top candidate is unimpeachable.
Boards are APPROVED by now, so reopen -> assign -> re-approve, then regenerate
history separately. Excludes dup-entangled names (Ngongba/Avdalas/Krivas/
Veesaar/Brown/Boozer/Sarr/Peterson) and genuinely-new full names.
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

ASSIGN: dict[str, int] = {
    "Ament": 5561,  # Nate Ament
    "Philon": 1709,  # Labaron Philon
    "Stirtz": 5578,  # Bennett Stirtz
    "Steinbach": 5456,  # Hannes Steinbach
    "Quaintance": 5478,  # Jayden Quaintance
    "Mara": 5577,  # Aday Mara
    "Dybantsa": 5390,  # AJ Dybantsa
    "Flemings": 5708,  # Kingston Flemings
    "De Larrea": 5416,  # Sergio de Larrea
    "Haugh": 5379,  # Thomas Haugh
    "Bidunga": 5625,  # Flory Bidunga
}


def _load_all_schemas() -> None:
    import importlib
    import pkgutil

    import app.schemas as schemas_pkg

    for mod in pkgutil.iter_modules(schemas_pkg.__path__):
        importlib.import_module(f"app.schemas.{mod.name}")


async def main() -> None:
    _load_all_schemas()
    from app.schemas.boards import BoardEntry, BoardStatus
    from app.services import board_service as svc

    engine = create_async_engine(
        os.environ["DATABASE_URL"], echo=False, pool_pre_ping=True
    )
    sf = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    tally = {"assigned": 0, "skipped": 0}
    reopened: set[int] = set()
    try:
        async with sf() as db:
            rows = (
                await db.execute(
                    select(BoardEntry.id, BoardEntry.board_id, BoardEntry.raw_name)  # type: ignore[call-overload]
                    .where(BoardEntry.player_id.is_(None))  # type: ignore[union-attr]
                    .where(BoardEntry.board_id >= 10)  # type: ignore[arg-type]
                )
            ).all()
            for entry_id, board_id, raw_name in rows:
                if raw_name not in ASSIGN:
                    continue
                try:
                    board = await svc.get_board(db, board_id)
                    if (
                        board.status is BoardStatus.APPROVED
                        and board_id not in reopened
                    ):
                        await svc.reopen_board(db, board_id=board_id)
                        reopened.add(board_id)
                    await svc.assign_entry(
                        db, entry_id=entry_id, player_id=ASSIGN[raw_name]
                    )
                    await db.commit()
                    tally["assigned"] += 1
                    print(
                        f"[assign] '{raw_name}' (board {board_id}) -> {ASSIGN[raw_name]}"
                    )
                except Exception as exc:  # noqa: BLE001
                    await db.rollback()
                    tally["skipped"] += 1
                    print(
                        f"[skip]   '{raw_name}' board={board_id}: {type(exc).__name__}: {str(exc)[:70]}"
                    )
            # Re-approve every board we reopened.
            for bid in reopened:
                await svc.approve_board(db, board_id=bid, recompute_consensus=False)
                await db.commit()
            print(f"\nreopened+reapproved boards: {sorted(reopened)}")
    finally:
        await engine.dispose()
    print("=== tally ===")
    print(f"  {tally}")


if __name__ == "__main__":
    asyncio.run(main())
