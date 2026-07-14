"""Merge duplicate/garbage player records surfaced by the 2025 draft-class audit.

Auditing why Noa Essengue/Johni Broome showed up in the homepage "Undrafted"
tracker (`sync_draft_positions` gap, fixed separately via
`scripts/data/draft_results_2025.txt`) turned up two distinct sources of
duplicate `players_master` rows for the 2025 class:

1. **Mis-parsed name fragments.** The Player Tags mention-extraction pipeline
   (`bio_source='ai_generated'`) minted bare-surname stubs -- "Edgecombe",
   "Harper", "Knueppel", "Queen" -- instead of resolving to the existing full
   record. Confirmed same person via an independent signal (the stub's
   `school` field matches the canonical record's school), not name similarity
   alone.
2. **BBRef/combine-import duplicates carrying real data.** `yangha01` (a
   basketball-reference player-ID string used as a display name), the
   unaccented "Nolan Traore", and "Hugo (Excused - Not in Chicago) Gonzalez"
   (a scraped combine-invite annotation folded into the name field) are not
   throwaway stubs -- they hold real `player_metric_values`/mentions that
   need reassigning, not discarding.

Also includes several unrelated-to-the-draft-picks-list dup pairs noticed
during the same audit (spelling-variant stubs for undrafted 2025 UDFAs).

One pair intentionally excluded: "Eli John N'Diaye" (5492) vs "Eli Ndiaye"
(5712) -- both are equally-thin `ai_generated` stubs and there is no
canonical record or independent signal to say which spelling is correct.
Left for manual review rather than guessed.

Reuses the proven merge machinery in `scripts/top100/merge_players.py`
(same pattern as `scripts/merge_may26_dup_players.py`). Deliberately does
NOT call `_update_keep_player` -- every keep record already has the correct
draft_year/draft_round/draft_pick (synced separately) and must not be
rewritten.

**Resolves by exact `display_name`, not hardcoded id.** Dev and prod
`players_master` rows for the same person carry different surrogate ids
(confirmed while auditing this class), so hardcoding the ids found in dev
would be wrong -- and silently dangerous -- if this script were ever pointed
at prod. A name that resolves to zero rows in the target DB is skipped
("already absent"); a name that resolves to more than one row is skipped
with an AMBIGUOUS warning rather than guessed, matching this repo's
never-guess entity-resolution convention. Safe to run against either DB by
pointing `DATABASE_URL` at it.

Usage:
    scripts/with-db-env.sh conda run -n draftguru python scripts/merge_2025_draft_dup_players.py
    scripts/with-db-env.sh conda run -n draftguru python scripts/merge_2025_draft_dup_players.py --execute
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

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

# board_entries (consensus mock-draft board rows) has a `players_master` FK not
# covered by the top100 merge tool's CHILD_TABLES -- discovered when the first
# --execute attempt hit `board_entries_player_id_fkey`. Unique on (board_id,
# player_id), so it needs the same conflict-then-reassign handling as the
# tool's other conflict_columns tables.
_BOARD_ENTRIES_TABLE = _mp.ChildTable("board_entries", "player_id", ("board_id",))

# Summer League child tables -- also not in the top100 tool's CHILD_TABLES
# (that tool predates the SL feature). Needed because the "yangha01" dup
# (3074) turns out to hold REAL Summer League data (game logs, shot events,
# PBP events, season aggregates, desk grades) under a bbref-ID-as-name
# record that split off from the canonical "Hansen Yang" (1730) row -- not a
# throwaway stub. Only `summer_league_player_seasons` has a player-scoped
# unique constraint (competition_id, player_id); a conflict there is safe to
# drop since season aggregates are re-derived from game logs by the normal
# metrics rebuild, never hand-authored.
_SL_CHILD_TABLES: tuple[Any, ...] = (
    _mp.ChildTable("player_affiliations", "player_id"),
    _mp.ChildTable(
        "summer_league_desk_player_grades",
        "player_id",
        ("competition_id", "baseline_version"),
    ),
    _mp.ChildTable("summer_league_participation", "player_id"),
    _mp.ChildTable("summer_league_player_game_logs", "player_id"),
    _mp.ChildTable("summer_league_player_seasons", "player_id", ("competition_id",)),
    _mp.ChildTable("summer_league_shot_events", "player_id"),
    _mp.ChildTable("summer_league_source_players", "canonical_player_id"),
    _mp.ChildTable("summer_league_play_by_play_events", "person1_id"),
    _mp.ChildTable("summer_league_play_by_play_events", "person2_id"),
    _mp.ChildTable("summer_league_play_by_play_events", "person3_id"),
)

# (discard_display_name, keep_display_name, reason) -- resolved to whichever
# DB's own ids at run time; see module docstring for why this isn't
# hardcoded ids.
MERGES: tuple[tuple[str, str, str], ...] = (
    # Mention-extraction bare-surname stubs -- confirmed via matching `school`.
    ("Edgecombe", "VJ Edgecombe", "stub (school=Baylor) -> VJ Edgecombe"),
    ("V. J. Edgecombe", "VJ Edgecombe", "stub (school=Baylor) -> VJ Edgecombe"),
    ("V.J. Edgecombe", "VJ Edgecombe", "stub (school=Baylor) -> VJ Edgecombe"),
    ("Harper", "Dylan Harper", "stub (school=Rutgers) -> Dylan Harper"),
    ("Knueppel", "Kon Knueppel", "stub (school=Duke) -> Kon Knueppel"),
    ("Queen", "Derik Queen", "stub (school=Maryland) -> Derik Queen"),
    (
        "Kasparas Jakucionius",
        "Kasparas Jakucionis",
        "stub (school=Illinois) -> Kasparas Jakucionis",
    ),
    (
        "Kasparas Jakucions",
        "Kasparas Jakucionis",
        "stub (school=Illinois) -> Kasparas Jakucionis",
    ),
    ("Joan Berringer", "Joan Beringer", "typo stub -> Joan Beringer"),
    ("Hugo Gonzalez", "Hugo González", "stub (Real Madrid) -> Hugo González"),
    # BBRef/combine-import duplicates carrying real data (not throwaway stubs).
    (
        "Hugo (Excused - Not in Chicago) Gonzalez",
        "Hugo González",
        "corrupted-name record -> Hugo González",
    ),
    (
        "Nolan Traore",
        "Nolan Traoré",
        "unaccented combine-import dup -> Nolan Traoré",
    ),
    (
        "yangha01",
        "Hansen Yang",
        "BBRef-ID-as-name record (stale wrong draft_team) -> Hansen Yang",
    ),
    # Unrelated-to-the-picks-list dup pairs, same class, same audit pass.
    ("Vlad Goldin", "Vladislav Goldin", "stub (school=Michigan) -> Vladislav Goldin"),
    ("RJ Luis", "RJ Luis Jr.", "stub (school=St. John's) -> RJ Luis Jr."),
    (
        "Cliff Omoruyi",
        "Clifford Omoruyi",
        "stub -> stub (fuller name, same school)",
    ),
    (
        "Igor Milcic Jr",
        "Igor Milicic Jr.",
        "typo stub -> correct spelling (more mentions)",
    ),
    (
        "Viktor Lahkin",
        "Viktor Lakhin",
        "typo stub -> correct transliteration (more mentions)",
    ),
    ("Adama Alpha-Bal", "Adama Bal", "stub -> stub (same school, simpler name)"),
    ("Wesley Cardet Jr", "Wesley Cardet Jr.", "stub -> stub (more mentions)"),
)


async def _resolve_id_by_name(conn: Any, display_name: str) -> tuple[int | None, bool]:
    """Look up a player id by exact `display_name`.

    Returns `(id, ambiguous)`. `(None, False)` means no match (already
    absent/never existed in this DB -- fine to skip). `(None, True)` means
    more than one row shares this exact name -- never guessed, reported.
    """
    rows = (
        await conn.execute(
            text("SELECT id FROM players_master WHERE display_name = :name"),
            {"name": display_name},
        )
    ).fetchall()
    if len(rows) == 1:
        return int(rows[0][0]), False
    if len(rows) == 0:
        return None, False
    return None, True


async def _table_exists(conn: Any, table: str) -> bool:
    """Return True when `table` exists in the public schema."""
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
        # player_embeddings exists in dev but not prod (per
        # merge_prod_shooting_dup_players.py); an unguarded query against it
        # raises UndefinedTable on prod, even in dry-run mode.
        has_embeddings = await _table_exists(conn, "player_embeddings")
        if not has_embeddings:
            print(
                "note: player_embeddings table absent (prod) — skipping embeddings step"
            )

        for discard_name, keep_name, reason in MERGES:
            discard_id, discard_ambiguous = await _resolve_id_by_name(
                conn, discard_name
            )
            if discard_ambiguous:
                print(
                    f"\nSKIP: {discard_name!r} matches multiple players — resolve manually"
                )
                continue
            if discard_id is None:
                print(f"\nskip {discard_name!r}: already absent")
                continue

            keep_id, keep_ambiguous = await _resolve_id_by_name(conn, keep_name)
            if keep_ambiguous:
                print(
                    f"\nSKIP: keep name {keep_name!r} matches multiple players — refusing to orphan {discard_name!r}"
                )
                continue
            if keep_id is None:
                print(
                    f"\nSKIP: keep {keep_name!r} not found — refusing to orphan {discard_name!r}"
                )
                continue

            print(
                f"\n{'[DRY RUN] ' if dry_run else ''}merge {discard_id} ({discard_name}) -> {keep_id} ({keep_name})"
            )
            print(f"  reason: {reason}")

            self_links = await _mp._delete_similarity_self_links(
                conn, keep_id=keep_id, discard_id=discard_id, dry_run=dry_run
            )
            if self_links:
                print(
                    f"    player_similarity self/conflicting keep links: delete {self_links}"
                )

            for spec in (
                *_mp.CHILD_TABLES,
                *_mp.SIMILARITY_TABLES,
                _BOARD_ENTRIES_TABLE,
                *_SL_CHILD_TABLES,
            ):
                affected, deleted, reassigned = await _mp._merge_child_table(
                    conn, spec, keep_id=keep_id, discard_id=discard_id, dry_run=dry_run
                )
                if affected:
                    print(
                        f"    {spec.table}.{spec.player_column}: "
                        f"affected={affected}, delete_conflicts={deleted}, reassign={reassigned}"
                    )

            # player_embeddings is NOT in the merge tool's CHILD_TABLES, but it has a
            # (non-cascade) FK to players_master and most dups own one. The canonical
            # already has its own embedding and player_embeddings.player_id is unique,
            # so we DELETE the dup's embedding rather than reassign it. Without this the
            # final players_master delete would hit an FK violation. Guarded because
            # the table doesn't exist on prod.
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
                        f"    player_embeddings.player_id: affected={emb}, delete={emb} (not in merge tool)"
                    )
                    if not dry_run:
                        await conn.execute(
                            text("DELETE FROM player_embeddings WHERE player_id = :d"),
                            {"d": discard_id},
                        )

            if dry_run:
                print(
                    f"    WOULD ADD alias {discard_name!r} -> {keep_id}; WOULD DELETE player {discard_id}"
                )
            else:
                await _mp._ensure_alias(
                    conn, keep_id, discard_name, "2025_draft_class_dedup"
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
        description="Merge 2025 draft-class duplicate/stub player records"
    )
    parser.add_argument("--execute", action="store_true", help="Apply changes")
    args = parser.parse_args()
    asyncio.run(run(dry_run=not args.execute))


if __name__ == "__main__":
    main()
