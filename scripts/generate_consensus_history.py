#!/usr/bin/env python
"""Phase 4 driver: generate real weekly consensus history from approved boards.

Once historical boards are ingested (``backfill_boards.py``) and APPROVED, this
replays ``recompute_consensus`` at weekly ``as_of`` ceilings from the earliest
approved board to now. Because the as-of engine selects the most-recent
approved board per source published at or before each ceiling, the resulting
snapshots reconstruct what the consensus *actually* looked like week by week —
real sparklines, real movers, real freshness — with rank deltas chained
correctly between consecutive snapshots.

Generation runs oldest-first so each snapshot's ``prev_rank`` anchors on the
chronologically-preceding one.

Destructive options are opt-in:
  * ``--purge-synthetic`` removes the demo snapshots seeded by
    ``seed_synthetic_consensus_history.py`` (``num_boards=0 AND trigger=MANUAL``).
  * ``--reset`` removes ALL snapshots for the draft year first, for a clean
    regenerable weekly series. Use when you want the history to be exactly the
    generated set and nothing else.

Usage (wrap with scripts/with-db-env.sh):
    # dry run — show the weekly ceilings that would be computed
    ... python scripts/generate_consensus_history.py --dry-run
    # clean regenerate weekly 2026 history from real boards
    ... python scripts/generate_consensus_history.py --reset --purge-synthetic
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# Make the co-located app package (this worktree/repo) win over any editable
# install, so `python scripts/...` imports this worktree's code — including the
# as_of-aware recompute_consensus this driver depends on.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_all_schemas() -> None:
    """Import every app.schemas submodule so FK metadata resolves."""
    import importlib
    import pkgutil

    import app.schemas as schemas_pkg

    for mod in pkgutil.iter_modules(schemas_pkg.__path__):
        importlib.import_module(f"app.schemas.{mod.name}")


def _weekly_ceilings(
    start: datetime, end: datetime, interval_days: int
) -> list[datetime]:
    """Ascending list of ceilings from start to end, end always included."""
    if end < start:
        return []
    out: list[datetime] = []
    cur = start
    step = timedelta(days=interval_days)
    while cur < end:
        out.append(cur)
        cur = cur + step
    out.append(end)
    return out


async def _earliest_approved_board_date(
    db: AsyncSession, *, draft_year: int
) -> datetime | None:
    from app.schemas.boards import Board, BoardStatus

    return await db.scalar(
        select(func.min(Board.published_at))  # type: ignore[call-overload]
        .where(Board.status == BoardStatus.APPROVED)  # type: ignore[arg-type]
        .where(Board.draft_year == draft_year)  # type: ignore[arg-type]
    )


async def generate_history(
    db: AsyncSession,
    *,
    draft_year: int,
    start: datetime,
    end: datetime,
    interval_days: int = 7,
    purge_synthetic: bool = False,
    reset: bool = False,
) -> list:
    """Generate weekly as-of snapshots for one draft year, oldest-first.

    Returns the list of created snapshots. The caller owns the commit.
    """
    from app.schemas.consensus import ConsensusSnapshot, ConsensusTrigger
    from app.services.consensus_service import recompute_consensus

    if reset:
        await db.execute(
            delete(ConsensusSnapshot).where(
                ConsensusSnapshot.draft_year == draft_year  # type: ignore[arg-type]
            )
        )
        await db.flush()
    elif purge_synthetic:
        await db.execute(
            delete(ConsensusSnapshot)
            .where(ConsensusSnapshot.draft_year == draft_year)  # type: ignore[arg-type]
            .where(ConsensusSnapshot.num_boards == 0)  # type: ignore[arg-type]
            .where(ConsensusSnapshot.trigger == ConsensusTrigger.MANUAL)  # type: ignore[arg-type]
        )
        await db.flush()

    snapshots = []
    for ceiling in _weekly_ceilings(start, end, interval_days):
        snap = await recompute_consensus(
            db,
            draft_year=draft_year,
            trigger=ConsensusTrigger.SCHEDULED,
            as_of=ceiling,
        )
        snapshots.append(snap)
    return snapshots


async def _run(args: argparse.Namespace) -> None:
    _load_all_schemas()
    engine = create_async_engine(os.environ["DATABASE_URL"], echo=False)
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    try:
        async with session_factory() as db:
            start = (
                datetime.fromisoformat(args.start)
                if args.start
                else await _earliest_approved_board_date(db, draft_year=args.draft_year)
            )
            if start is None:
                print(
                    f"no APPROVED boards for {args.draft_year}; "
                    "approve backfilled boards first"
                )
                return
            end = datetime.fromisoformat(args.end) if args.end else datetime.utcnow()
            ceilings = _weekly_ceilings(start, end, args.interval_days)
            print(
                f"draft_year={args.draft_year} start={start.date()} "
                f"end={end.date()} interval={args.interval_days}d "
                f"-> {len(ceilings)} weekly snapshots"
            )
            if args.dry_run:
                for c in ceilings:
                    print(f"  as_of {c.date()}")
                print("\n(dry run — no writes)")
                return

            snaps = await generate_history(
                db,
                draft_year=args.draft_year,
                start=start,
                end=end,
                interval_days=args.interval_days,
                purge_synthetic=args.purge_synthetic,
                reset=args.reset,
            )
            await db.commit()
            print(f"created {len(snaps)} snapshots")
            for s in snaps:
                print(
                    f"  snapshot id={s.id} as_of={s.computed_at.date()} boards={s.num_boards}"
                )
    finally:
        await engine.dispose()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--draft-year", type=int, default=2026)
    ap.add_argument("--start", help="ISO date; default = earliest approved board")
    ap.add_argument("--end", help="ISO date; default = now")
    ap.add_argument("--interval-days", type=int, default=7)
    ap.add_argument(
        "--purge-synthetic",
        action="store_true",
        help="delete demo synthetic snapshots (num_boards=0, MANUAL) first",
    )
    ap.add_argument(
        "--reset",
        action="store_true",
        help="delete ALL snapshots for the year first (clean regenerate)",
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if "DATABASE_URL" not in os.environ and not args.dry_run:
        print("ERROR: DATABASE_URL not set (wrap with scripts/with-db-env.sh)")
        sys.exit(1)
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
