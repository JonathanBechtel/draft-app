"""Merge the May-26 combine-shooting duplicate players into canonicals — PRODUCTION.

This is the production counterpart to ``scripts/merge_may26_dup_players.py``. The
2026-05-26 combine-shooting import created duplicate player rows in *both* dev and
prod (name matching choked on the "Jr." suffix and on initials, inserting rows like
"Jr. Darius Acuff" / "A.J. Dybantsa" with mangled slugs). The dev cleanup ran with
dev-era ids (6185-6189); prod's orphans carry *different* surrogate ids, so the id
map below is rebuilt against production.

Two production-specific differences from the dev script:

1. **IDs differ.** Prod orphans are 6444-6447; their canonicals are resolved below.
   Prod's Jeremy Fears canonical is 6433 (draft 2026, holds combine anthro/agility +
   similarity) — NOT the ai_generated stub 6344 (draft 2027, zero attached data),
   which is a separate record left untouched for manual review.
2. **No ``player_embeddings`` table.** The embeddings feature is not deployed to prod,
   so the embeddings delete the dev script performs is skipped here (guarded).

Each orphan holds only one ``combine_shooting_results`` row plus shooting/composite
metric values and similarity; everything is re-pointed onto the canonical and the
orphan is deleted. Canonical records are NOT rewritten.

Note: the canonical players' ``composite`` similarity was computed before shooting
existed on them, so after this merge a shooting + composite metrics/similarity
recompute for the affected canonicals is recommended for fully consistent composite
scores (the shooting view itself is restored by the reassignment alone).

Usage:
    DATABASE_URL=<prod> conda run -n draftguru python scripts/merge_prod_shooting_dup_players.py
    DATABASE_URL=<prod> conda run -n draftguru python scripts/merge_prod_shooting_dup_players.py --execute
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import os
import sys
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Load the top100 merge module by path (scripts/ is not a package).
_spec = importlib.util.spec_from_file_location(
    "_top100_merge", REPO_ROOT / "scripts" / "top100" / "merge_players.py"
)
assert _spec and _spec.loader
_mp = importlib.util.module_from_spec(_spec)
sys.modules["_top100_merge"] = _mp  # required so @dataclass(slots=True) can resolve
_spec.loader.exec_module(_mp)

# (discard_id, keep_id) — discard is the prod May-26 shooting dup, keep is canonical.
MERGES: tuple[tuple[int, int], ...] = (
    (6444, 5428),  # "Jr. Morez Johnson" -> "Morez Johnson Jr." (2026)
    (6445, 5384),  # "Jr. Darius Acuff"  -> "Darius Acuff Jr."  (2026)
    (6446, 6433),  # "Jr. Jeremy Fears"  -> "Jeremy Fears Jr."  (2026, has combine)
    (6447, 5390),  # "A.J. Dybantsa"     -> "AJ Dybantsa"       (2026)
)


async def _table_exists(conn, table: str) -> bool:
    """Return True when ``table`` exists in the public schema."""
    found = (
        await conn.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = :t"
            ),
            {"t": table},
        )
    ).scalar()
    return bool(found)


async def run(dry_run: bool) -> None:
    url, connect_args = _mp._prepare_connection(os.environ["DATABASE_URL"])
    engine = create_async_engine(url, echo=False, connect_args=connect_args)
    print(f"Mode: {'DRY RUN' if dry_run else 'EXECUTE'}")

    async with engine.begin() as conn:
        has_embeddings = await _table_exists(conn, "player_embeddings")
        if not has_embeddings:
            print(
                "note: player_embeddings table absent (prod) — skipping embeddings step"
            )

        for discard_id, keep_id in MERGES:
            discard_name = await _mp._fetch_display_name(conn, discard_id)
            keep_name = await _mp._fetch_display_name(conn, keep_id)
            if discard_name is None:
                print(f"\nskip {discard_id}: already absent")
                continue
            if keep_name is None:
                print(
                    f"\nSKIP: keep id {keep_id} not found — refusing to orphan {discard_id}"
                )
                continue

            print(
                f"\n{'[DRY RUN] ' if dry_run else ''}merge {discard_id} ({discard_name}) "
                f"-> {keep_id} ({keep_name})"
            )

            self_links = await _mp._delete_similarity_self_links(
                conn, keep_id=keep_id, discard_id=discard_id, dry_run=dry_run
            )
            if self_links:
                print(
                    f"    player_similarity self/conflicting keep links: delete {self_links}"
                )

            for spec in (*_mp.CHILD_TABLES, *_mp.SIMILARITY_TABLES):
                affected, deleted, reassigned = await _mp._merge_child_table(
                    conn, spec, keep_id=keep_id, discard_id=discard_id, dry_run=dry_run
                )
                if affected:
                    print(
                        f"    {spec.table}.{spec.player_column}: "
                        f"affected={affected}, delete_conflicts={deleted}, reassign={reassigned}"
                    )

            # player_embeddings exists in dev but not prod; guard it so the final
            # players_master delete does not hit an FK violation where it does exist.
            if has_embeddings:
                emb = (
                    await conn.execute(
                        text(
                            "SELECT count(*) FROM player_embeddings WHERE player_id = :d"
                        ),
                        {"d": discard_id},
                    )
                ).scalar()
                if emb:
                    print(
                        f"    player_embeddings.player_id: affected={emb}, delete={emb}"
                    )
                    if not dry_run:
                        await conn.execute(
                            text("DELETE FROM player_embeddings WHERE player_id = :d"),
                            {"d": discard_id},
                        )

            if dry_run:
                print(
                    f"    WOULD ADD alias {discard_name!r} -> {keep_id}; "
                    f"WOULD DELETE player {discard_id}"
                )
            else:
                await _mp._ensure_alias(
                    conn, keep_id, discard_name, "may26_combine_import_dedup"
                )
                await conn.execute(
                    text("DELETE FROM players_master WHERE id = :discard_id"),
                    {"discard_id": discard_id},
                )
                print(f"    deleted player {discard_id}")

        if dry_run:
            await conn.rollback()
            print("\nDry run complete; transaction rolled back.")
        else:
            print("\nMerge execution committed.")

    await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Merge prod May-26 combine-shooting dup players"
    )
    parser.add_argument("--execute", action="store_true", help="Apply changes")
    args = parser.parse_args()
    asyncio.run(run(dry_run=not args.execute))


if __name__ == "__main__":
    main()
