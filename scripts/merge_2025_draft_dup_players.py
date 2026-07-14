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

# (discard_id, keep_id, reason)
MERGES: tuple[tuple[int, int, str], ...] = (
    # Mention-extraction bare-surname stubs -- confirmed via matching `school`.
    (5720, 1673, "'Edgecombe' stub (school=Baylor) -> VJ Edgecombe"),
    (5394, 1673, "'V. J. Edgecombe' stub (school=Baylor) -> VJ Edgecombe"),
    (5404, 1673, "'V.J. Edgecombe' stub (school=Baylor) -> VJ Edgecombe"),
    (5724, 1682, "'Harper' stub (school=Rutgers) -> Dylan Harper"),
    (5719, 1689, "'Knueppel' stub (school=Duke) -> Kon Knueppel"),
    (5725, 1712, "'Queen' stub (school=Maryland) -> Derik Queen"),
    (
        5831,
        1684,
        "'Kasparas Jakucionius' stub (school=Illinois) -> Kasparas Jakucionis",
    ),
    (5687, 1684, "'Kasparas Jakucions' stub (school=Illinois) -> Kasparas Jakucionis"),
    (6296, 1663, "'Joan Berringer' typo stub -> Joan Beringer"),
    (5601, 1680, "'Hugo Gonzalez' stub (Real Madrid) -> Hugo González"),
    # BBRef/combine-import duplicates carrying real data (not throwaway stubs).
    (
        1748,
        1680,
        "'Hugo (Excused - Not in Chicago) Gonzalez' corrupted-name record -> Hugo González",
    ),
    (1745, 1725, "Unaccented 'Nolan Traore' combine-import dup -> Nolan Traoré"),
    (
        3074,
        1730,
        "'yangha01' BBRef-ID-as-name record (stale wrong draft_team) -> Hansen Yang",
    ),
    # Unrelated-to-the-picks-list dup pairs, same class, same audit pass.
    (5880, 1679, "'Vlad Goldin' stub (school=Michigan) -> Vladislav Goldin"),
    (5539, 1692, "'RJ Luis' stub (school=St. John's) -> RJ Luis Jr."),
    (
        5806,
        5511,
        "'Cliff Omoruyi' stub -> 'Clifford Omoruyi' stub (fuller name, same school)",
    ),
    (
        5829,
        5525,
        "'Igor Milcic Jr' typo stub -> 'Igor Milicic Jr.' (correct spelling, more mentions)",
    ),
    (
        5508,
        5445,
        "'Viktor Lahkin' typo stub -> 'Viktor Lakhin' (correct transliteration, more mentions)",
    ),
    (5630, 5850, "'Adama Alpha-Bal' stub -> 'Adama Bal' (same school, simpler name)"),
    (5486, 5594, "'Wesley Cardet Jr' stub -> 'Wesley Cardet Jr.' (more mentions)"),
)


async def run(dry_run: bool) -> None:
    url, connect_args = _mp._prepare_connection(os.environ["DATABASE_URL"])
    engine = create_async_engine(url, echo=False, connect_args=connect_args)
    print(f"Mode: {'DRY RUN' if dry_run else 'EXECUTE'}")

    async with engine.begin() as conn:
        for discard_id, keep_id, reason in MERGES:
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
