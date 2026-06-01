"""Backfill ``player_status.position_id`` from existing ``raw_position`` text.

Background
----------
``scripts/top100/cbb_enrich.py`` (the ``sports_reference_cbb`` source) used to
upsert ``player_status`` with ``raw_position`` but never resolved the
``position_id`` foreign key. The same-position similarity filter
(:func:`app.services.similarity_service._resolve_position_parents`) keys off
``position_id`` — so every CBB-enriched prospect silently lost position-adjusted
comps ("No similar players found for this view"). The ingest is now fixed; this
script repairs the rows already written.

It is safe and idempotent: it only touches rows where ``position_id IS NULL``
and ``raw_position`` resolves to a known fine code via the position taxonomy.
Unmappable labels (e.g. "Wing", "Combo Guard") are left untouched and reported
for manual review. No similarity/metric recompute is required — the comps rows
already exist; they are filtered by ``position_id`` at query time.

Usage (report only, no writes)::

    scripts/with-db-env.sh conda run -n draftguru \
        python scripts/backfill_player_status_position_id.py

Apply::

    scripts/with-db-env.sh conda run -n draftguru \
        python scripts/backfill_player_status_position_id.py --execute
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from pathlib import Path

from sqlalchemy import select

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.models.position_taxonomy import derive_position_tags  # noqa: E402
from app.schemas.player_status import PlayerStatus  # noqa: E402
from app.utils.db_async import SessionLocal  # noqa: E402
from scripts.ingest_combine import get_or_create_position_id  # noqa: E402


async def backfill(execute: bool) -> int:
    """Resolve and persist missing ``position_id`` values.

    Args:
        execute: When ``True`` commit the changes; otherwise dry-run.

    Returns:
        Process exit code (0 on success).
    """
    resolved = 0
    unmapped: Counter[str] = Counter()
    examples: list[str] = []

    async with SessionLocal() as session:
        stmt = (
            select(PlayerStatus)
            .where(PlayerStatus.position_id.is_(None))  # type: ignore[union-attr]
            .where(PlayerStatus.raw_position.is_not(None))  # type: ignore[union-attr]
        )
        rows = (await session.execute(stmt)).scalars().all()
        print(f"Candidate rows (position_id NULL, raw_position set): {len(rows)}")

        for status in rows:
            fine, _parents = derive_position_tags(status.raw_position)
            if not fine:
                unmapped[status.raw_position or ""] += 1
                continue
            position_id = await get_or_create_position_id(session, fine)
            status.position_id = position_id
            resolved += 1
            if len(examples) < 10:
                examples.append(
                    f"  player_id={status.player_id} {status.raw_position!r} -> {fine} (id={position_id})"
                )

        print(f"Resolved: {resolved}")
        if examples:
            print("Sample resolutions:")
            print("\n".join(examples))
        if unmapped:
            print("Unmapped raw_position values (left untouched, review manually):")
            for label, count in unmapped.most_common():
                print(f"  {label!r}: {count}")

        if execute:
            await session.commit()
            print(f"COMMITTED {resolved} updates.")
        else:
            await session.rollback()
            print("DRY RUN — no changes written. Re-run with --execute to apply.")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Commit changes (default is a dry run).",
    )
    args = parser.parse_args()
    return asyncio.run(backfill(execute=args.execute))


if __name__ == "__main__":
    raise SystemExit(main())
