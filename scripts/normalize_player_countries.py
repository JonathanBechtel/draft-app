r"""One-off maintenance: normalize ``players_master.birth_country`` to canonical names.

Player bios were ingested from several sources that disagree on encoding (ISO-2
codes like ``US``/``AU`` vs. full names like ``United States``/``Australia`` vs.
aliases like ``USA``/``U.S.``).  This collapses every value onto its canonical
display name via :func:`app.utils.country.canonical_country`, so the Explorer
country facet/filter sees one value per country.

Idempotent: rows already canonical are skipped.  Defaults to a dry run; pass
``--execute`` to commit.

Usage::

    scripts/with-db-env.sh conda run -n draftguru \\
        python scripts/normalize_player_countries.py            # dry run
    scripts/with-db-env.sh conda run -n draftguru \\
        python scripts/normalize_player_countries.py --execute  # commit
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from pathlib import Path

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.schemas.players_master import PlayerMaster  # noqa: E402
from app.utils.country import canonical_country  # noqa: E402
from app.utils.db_async import SessionLocal  # noqa: E402


async def run(execute: bool) -> None:
    """Rewrite non-canonical birth_country values; print a per-change summary."""
    changes: Counter[str] = Counter()
    updated = 0
    async with SessionLocal() as db:
        result = await db.execute(
            select(PlayerMaster).where(PlayerMaster.birth_country.isnot(None))  # type: ignore[union-attr]
        )
        players = result.scalars().all()
        for p in players:
            canonical = canonical_country(p.birth_country)
            if canonical is not None and canonical != p.birth_country:
                changes[f"{p.birth_country!r} -> {canonical!r}"] += 1
                p.birth_country = canonical
                updated += 1
        if execute:
            await db.commit()

    for change, n in sorted(changes.items(), key=lambda kv: -kv[1]):
        print(f"  {n:5d}  {change}")
    verb = "Updated" if execute else "Would update"
    print(f"\n{verb} {updated} rows across {len(changes)} distinct mappings.")
    if not execute:
        print("(dry run — re-run with --execute to commit)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute", action="store_true", help="Commit changes (default: dry run)"
    )
    args = parser.parse_args()
    asyncio.run(run(args.execute))


if __name__ == "__main__":
    main()
