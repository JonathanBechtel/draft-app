"""Resolve punctuation/diacritic duplicate ``players_master`` rows.

The duplicate class
-------------------
Ingestion periodically creates a second player row that differs from the canonical one only
in punctuation: a curly apostrophe instead of a straight one (``Day'Ron`` / ``Day'Ron``),
initials with or without periods (``EJ`` / ``E.J.``), a suffix with or without a period
(``Mike Miles Jr`` / ``Mike Miles Jr.``), or a hyphen rendered as a space. The new row is a
stub — no external ids, no stats — while the canonical row holds everything.

Why this is not a name-matching script
--------------------------------------
**Normalized-name equality is not evidence of a duplicate**, and acting on it alone would be
a data-loss bug. Stripping punctuation collapses genuinely different people onto the same
key — in production it groups these father/son pairs:

===================  ==========================================  ==============
Key                  Rows                                        Reality
===================  ==========================================  ==============
``ronharperjr``      Ron Harper (1986 draft) / Ron Harper Jr.     two players
``scottypippenjr``   Scotty Pippen (1987 draft) / Scotty Pippen Jr. two players
``gregbrowniii``     Greg Brown (1957 draft) / Greg Brown III     two players
===================  ==========================================  ==============

This repo has already been burned by exactly that: the Basketball-Reference first-initial
merge that contaminated Derek Harper's bio with Dylan Harper's. So name similarity only
selects *candidates*; a candidate is merged only on positive evidence, and anything
ambiguous is reported and left alone rather than guessed at.

The safe pattern
----------------
A group is auto-merged only when all of these hold:

1. exactly two rows;
2. one is ``is_stub`` with **no external ids and nothing in a table the merge service cannot
   reassign** — it carries no identity of its own, and nothing that would strand mid-merge;
3. the other is a non-stub with at least one external id — a real, identified player;
4. their ``draft_year`` values do not disagree.

Anything else is reported under ``DIFFERENT PEOPLE`` (conflicting external ids in the same
system, or both sides identified) or ``REVIEW`` (everything the rules cannot settle), and is
never touched.

Merge direction is taken from **where the data is**, never from the id or the name style —
in production ``AJ Lawson`` (id 1402) is the empty row while ``A.J. Lawson`` (id 1733) holds
263 rows, which is the reverse of the usual ordering.

Usage::

    scripts/with-db-env.sh conda run -n draftguru python scripts/resolve_punctuation_dup_players.py
    scripts/with-db-env.sh conda run -n draftguru python scripts/resolve_punctuation_dup_players.py --execute

Dry run is the default and prints the full classification. Point it at another database with
``DATABASE_URL``; ``--database-url`` overrides.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.player_merge_service import merge_players, preview_merge  # noqa: E402


# Columns the merge service CANNOT reassign — the unclassified edges tracked by
# tests/unit/test_player_merge_fk_coverage.py. A discard row holding any of these would
# hard-fail the merge on a RESTRICT FK, so this set decides whether a merge is safe at all.
_BLOCKING_REFERENCES: tuple[tuple[str, str], ...] = (
    ("draft_results", "player_id"),
    ("player_affiliations", "player_id"),
    ("summer_league_participation", "player_id"),
    ("summer_league_player_game_logs", "player_id"),
    ("summer_league_shot_events", "player_id"),
    ("summer_league_play_by_play_events", "person1_id"),
    ("summer_league_play_by_play_events", "person2_id"),
    ("summer_league_play_by_play_events", "person3_id"),
    ("summer_league_source_players", "canonical_player_id"),
    ("summer_league_player_resolution_reviews", "selected_player_id"),
    ("summer_league_desk_player_grades", "player_id"),
    ("summer_league_desk_storylines", "subject_player_id"),
    ("summer_league_desk_storylines", "subject_player_id_2"),
)

# Columns the merge service reassigns for us. Rows here are fine on either side — they
# simply move to the survivor — so they inform the report but never block a merge.
_MOVABLE_REFERENCES: tuple[tuple[str, str], ...] = (
    ("player_aliases", "player_id"),
    ("player_bio_snapshots", "player_id"),
    ("player_college_stats", "player_id"),
    ("player_content_mentions", "player_id"),
    ("player_enrichment_jobs", "player_id"),
    ("player_external_ids", "player_id"),
    ("player_image_assets", "player_id"),
    ("player_lifecycle", "player_id"),
    ("player_metric_values", "player_id"),
    ("player_status", "player_id"),
    ("big_board_consensus", "player_id"),
    ("board_entries", "player_id"),
    ("combine_agility", "player_id"),
    ("combine_anthro", "player_id"),
    ("combine_shooting_results", "player_id"),
    ("news_items", "player_id"),
    ("podcast_episodes", "player_id"),
)

SAFE = "SAFE MERGE"
DIFFERENT = "DIFFERENT PEOPLE"
REVIEW = "REVIEW"


@dataclass
class Candidate:
    """One ``players_master`` row inside a name-collision group."""

    player_id: int
    display_name: str
    draft_year: int | None
    is_stub: bool
    external_ids: set[tuple[str, str]] = field(default_factory=set)
    blocking_rows: int = 0
    movable_rows: int = 0

    @property
    def identified(self) -> bool:
        """True when the row carries at least one external id."""
        return bool(self.external_ids)

    @property
    def child_rows(self) -> int:
        """Total child rows, for display."""
        return self.blocking_rows + self.movable_rows


@dataclass
class Group:
    """A set of rows whose display names match once punctuation is removed."""

    key: str
    members: list[Candidate]
    verdict: str = REVIEW
    reason: str = ""
    keep_id: int | None = None
    discard_id: int | None = None


async def _load_groups(conn) -> list[Group]:
    """Return every name-collision group with the evidence needed to classify it."""
    rows = (
        await conn.execute(
            text(
                """
                WITH k AS (
                    SELECT id, lower(regexp_replace(display_name, '[^a-zA-Z]', '', 'g')) AS nk
                    FROM players_master
                ),
                grp AS (SELECT nk FROM k GROUP BY nk HAVING count(*) > 1)
                SELECT k.nk, p.id, p.display_name, p.draft_year, p.is_stub
                FROM players_master p
                JOIN k ON k.id = p.id
                JOIN grp ON grp.nk = k.nk
                ORDER BY k.nk, p.id
                """
            )
        )
    ).all()

    grouped: dict[str, list[Candidate]] = {}
    for key, player_id, name, draft_year, is_stub in rows:
        grouped.setdefault(key, []).append(
            Candidate(player_id, name, draft_year, bool(is_stub))
        )

    for members in grouped.values():
        for member in members:
            member.external_ids = {
                (system, value)
                for system, value in (
                    await conn.execute(
                        text(
                            "SELECT system, external_id FROM player_external_ids "
                            "WHERE player_id = :pid"
                        ),
                        {"pid": member.player_id},
                    )
                ).all()
            }

            async def _count(refs) -> int:
                total = 0
                for table, column in refs:
                    total += int(
                        (
                            await conn.execute(
                                text(
                                    f"SELECT count(*) FROM {table} WHERE {column} = :pid"
                                ),
                                {"pid": member.player_id},
                            )
                        ).scalar()
                        or 0
                    )
                return total

            member.blocking_rows = await _count(_BLOCKING_REFERENCES)
            member.movable_rows = await _count(_MOVABLE_REFERENCES)

    return [Group(key, members) for key, members in sorted(grouped.items())]


def classify(group: Group) -> Group:
    """Decide whether a group is safe to merge, and in which direction.

    Errs toward REVIEW throughout: an unmerged duplicate is a cosmetic problem, while a
    wrongly merged pair destroys one player's record and is not cleanly reversible.
    """
    members = group.members
    if len(members) != 2:
        group.verdict, group.reason = REVIEW, f"{len(members)} rows in the group"
        return group

    first, second = members

    # Conflicting ids within one system prove these are different people. This is the
    # father/son case, and the check that keeps the namesake contamination from recurring.
    for system in {s for s, _ in first.external_ids} & {
        s for s, _ in second.external_ids
    }:
        left = {v for s, v in first.external_ids if s == system}
        right = {v for s, v in second.external_ids if s == system}
        if left and right and not left & right:
            group.verdict = DIFFERENT
            group.reason = f"conflicting {system} ids {sorted(left)} vs {sorted(right)}"
            return group

    if first.identified and second.identified:
        group.verdict = DIFFERENT
        group.reason = "both rows carry external ids — two identified players"
        return group

    known_years = {m.draft_year for m in members if m.draft_year is not None}
    if len(known_years) > 1:
        group.verdict = REVIEW
        group.reason = f"draft_year disagrees {sorted(known_years)}"
        return group

    # A discard is safe when it carries no identity of its own and nothing the merge
    # service is unable to move. Rows in reassignable tables are fine — relocating them is
    # exactly what the merge does.
    discardable = [
        m for m in members if m.is_stub and not m.identified and m.blocking_rows == 0
    ]
    identified = [m for m in members if not m.is_stub and m.identified]

    if len(discardable) == 1 and len(identified) == 1:
        discard, keep = discardable[0], identified[0]
        group.verdict = SAFE
        group.keep_id = keep.player_id
        group.discard_id = discard.player_id
        group.reason = (
            f"stub {discard.player_id} has no external ids and nothing in an "
            f"unreassignable table ({discard.movable_rows} movable row(s)); "
            f"{keep.player_id} is identified and holds {keep.child_rows} row(s)"
        )
        return group

    # A stub holding rows the merge cannot move would fail mid-operation — say so plainly
    # rather than filing it under a generic "does not match" verdict.
    stranded = [m for m in members if m.is_stub and m.blocking_rows]
    if stranded:
        group.verdict = REVIEW
        group.reason = (
            f"stub {stranded[0].player_id} holds {stranded[0].blocking_rows} row(s) the "
            "merge service cannot reassign — merging it would fail on a RESTRICT FK"
        )
        return group

    if all(m.is_stub and m.child_rows == 0 for m in members):
        group.verdict = REVIEW
        group.reason = (
            "both rows are empty stubs — probably duplicates, but nothing proves it"
        )
        return group

    group.verdict = REVIEW
    group.reason = "does not match the safe pattern"
    return group


def _render(groups: list[Group]) -> None:
    """Print the full classification so every decision is visible before anything runs."""
    for verdict in (SAFE, DIFFERENT, REVIEW):
        selected = [g for g in groups if g.verdict == verdict]
        print(f"\n{'=' * 72}\n{verdict}  ({len(selected)})\n{'=' * 72}")
        for group in selected:
            print(f"  {group.key}: {group.reason}")
            for member in group.members:
                role = ""
                if member.player_id == group.keep_id:
                    role = "  <- KEEP"
                elif member.player_id == group.discard_id:
                    role = "  <- DISCARD"
                print(
                    f"      id={member.player_id:<7} {member.display_name:<28} "
                    f"draft_year={member.draft_year} stub={member.is_stub} "
                    f"rows={member.child_rows:<6} ext_ids={len(member.external_ids)}{role}"
                )


async def run(*, dry_run: bool, database_url: str) -> int:
    """Classify every collision group and, with ``--execute``, merge the safe ones."""
    engine = create_async_engine(database_url, echo=False)
    try:
        async with engine.connect() as conn:
            groups = [classify(g) for g in await _load_groups(conn)]
            _render(groups)

            safe = [g for g in groups if g.verdict == SAFE]
            print(f"\n{'=' * 72}")
            print(
                f"Mode: {'DRY RUN' if dry_run else 'EXECUTE'} — {len(safe)} safe merge(s)"
            )
            print(f"{'=' * 72}")

            if not safe:
                print("Nothing to do.")
                return 0

            if dry_run:
                for group in safe:
                    assert group.keep_id is not None and group.discard_id is not None
                    report = await preview_merge(
                        conn, keep_id=group.keep_id, discard_id=group.discard_id
                    )
                    touched = {
                        t: c for t, c in report.per_table.items() if any(c.values())
                    }
                    print(
                        f"  [dry run] {group.discard_id} -> {group.keep_id} "
                        f"({group.key}): {touched or 'no child rows to move'}"
                    )
                print("\nRe-run with --execute to apply.")
                return 0

        # Each merge gets its own transaction, so one failure cannot undo the merges that
        # already succeeded. `engine.begin()` is what actually commits: the merge service
        # deliberately leaves commits to its caller (see CLAUDE.md, service-layer patterns),
        # so running it on a plain `connect()` silently rolls everything back on close.
        failures = 0
        for group in safe:
            assert group.keep_id is not None and group.discard_id is not None
            try:
                async with engine.begin() as conn:
                    await merge_players(
                        conn, keep_id=group.keep_id, discard_id=group.discard_id
                    )
            except Exception as exc:  # noqa: BLE001 - report and continue to the next pair
                failures += 1
                print(
                    f"  FAILED {group.discard_id} -> {group.keep_id} ({group.key}): {exc}"
                )
                continue

            # Confirm the write actually landed. Without this the script would report
            # success for a transaction that never committed — which is exactly what an
            # earlier version of it did.
            async with engine.connect() as conn:
                still_there = (
                    await conn.execute(
                        text("SELECT count(*) FROM players_master WHERE id = :pid"),
                        {"pid": group.discard_id},
                    )
                ).scalar()
            if still_there:
                failures += 1
                print(
                    f"  FAILED {group.discard_id} -> {group.keep_id} ({group.key}): "
                    "discard row still present after commit"
                )
            else:
                print(f"  merged {group.discard_id} -> {group.keep_id} ({group.key})")

        print(f"\n{len(safe) - failures} merged, {failures} failed.")
        return 1 if failures else 0
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Apply the safe merges. Without it the script only reports.",
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL"),
        help="Defaults to DATABASE_URL.",
    )
    args = parser.parse_args(argv)

    if not args.database_url:
        parser.error("no database URL: set DATABASE_URL or pass --database-url")

    url = args.database_url.strip('"')
    if "+asyncpg" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)

    return asyncio.run(run(dry_run=not args.execute, database_url=url))


if __name__ == "__main__":
    raise SystemExit(main())
