"""Merge the May-26 combine-import duplicate player records into their canonical rows.

A combine-shooting import on 2026-05-26 created 5 NEW player rows (ids 6185-6189)
for prospects that already existed canonically, because its name matching choked on
the "Jr." suffix (the new rows were inserted suffix-first, e.g. "Jr Morez Johnson",
which also produced mangled slugs like ``jr-morez-johnson``). The only unique data
those rows hold is one ``combine_shooting_results`` row each; everything else
(metrics, similarity, embeddings) is an inferior subset of the canonical record.

This reuses the proven merge machinery in ``scripts/top100/merge_players.py`` to
re-point child rows (combine results included) onto the canonical record and delete
the duplicate. It deliberately does NOT call ``_update_keep_player`` — the canonical
records are already correct and must not be rewritten.

Usage:
    scripts/with-db-env.sh conda run -n draftguru python scripts/merge_may26_dup_players.py
    scripts/with-db-env.sh conda run -n draftguru python scripts/merge_may26_dup_players.py --execute
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
sys.modules["_top100_merge"] = (
    _mp  # required so @dataclass(slots=True) can resolve its module
)
_spec.loader.exec_module(_mp)

# (discard_id, keep_id) — discard is the May-26 dup, keep is the canonical record.
MERGES: tuple[tuple[int, int], ...] = (
    (6185, 5529),  # Morez Johnson Jr.  -> canonical (Michigan)
    (6186, 5384),  # Darius Acuff Jr.   -> canonical (Arkansas)
    (6187, 6151),  # Jeremy Fears Jr.   -> canonical
    (6188, 5390),  # A.J. Dybantsa dup  -> canonical "AJ Dybantsa" (BYU)
    (
        6189,
        5382,
    ),  # "Christian Anderson Jr." dup -> canonical "Christian Anderson" (Texas Tech)
    # Pre-existing suffix-variant stub surfaced while verifying the above: the
    # suffix-less stub "Jeremy Fears" (6002) is the same person as canonical
    # "Jeremy Fears Jr." (6151) and blocked its resolution (suffix normalization
    # made the name match both). Unambiguous (single canonical match); the stub
    # only adds 2 content-mentions, which the merge preserves.
    (6002, 6151),  # "Jeremy Fears" stub -> canonical "Jeremy Fears Jr."
)


async def run(dry_run: bool) -> None:
    url, connect_args = _mp._prepare_connection(os.environ["DATABASE_URL"])
    engine = create_async_engine(url, echo=False, connect_args=connect_args)
    print(f"Mode: {'DRY RUN' if dry_run else 'EXECUTE'}")

    async with engine.begin() as conn:
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
                f"\n{'[DRY RUN] ' if dry_run else ''}merge {discard_id} ({discard_name}) -> {keep_id} ({keep_name})"
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

            # player_embeddings is NOT in the merge tool's CHILD_TABLES, but it has a
            # (non-cascade) FK to players_master and every dup owns one. The canonical
            # already has its own embedding and player_embeddings.player_id is unique,
            # so we DELETE the dup's embedding rather than reassign it. Without this the
            # final players_master delete would hit an FK violation.
            emb = (
                await conn.execute(
                    text("SELECT count(*) FROM player_embeddings WHERE player_id = :d"),
                    {"d": discard_id},
                )
            ).scalar()
            if emb:
                print(
                    f"    player_embeddings.player_id: affected={emb}, delete={emb} (not in merge tool)"
                )

            if dry_run:
                print(
                    f"    WOULD ADD alias {discard_name!r} -> {keep_id}; WOULD DELETE player {discard_id}"
                )
            else:
                await conn.execute(
                    text("DELETE FROM player_embeddings WHERE player_id = :d"),
                    {"d": discard_id},
                )
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
        description="Merge May-26 combine-import dup players"
    )
    parser.add_argument("--execute", action="store_true", help="Apply changes")
    args = parser.parse_args()
    asyncio.run(run(dry_run=not args.execute))


if __name__ == "__main__":
    main()
