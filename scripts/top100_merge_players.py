"""Apply reviewed dev-only player merges for the Top 100 refresh.

The default mode is a dry run. Use --execute only after reviewing the emitted
plan and affected child-row counts.

Usage:
    conda run -n draftguru python scripts/top100_merge_players.py
    conda run -n draftguru python scripts/top100_merge_players.py --execute
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import ssl
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.player_mention_service import parse_player_name  # noqa: E402


OUTPUT_DIR = Path("scraper/output")


@dataclass(frozen=True, slots=True)
class MergePlan:
    """Reviewed duplicate group to merge into one canonical record."""

    source_rank: int
    source_name: str
    keep_id: int
    discard_ids: tuple[int, ...]
    canonical_display_name: str
    canonical_school: str
    source_school_raw: str
    draft_year: int
    reason: str


@dataclass(frozen=True, slots=True)
class ChildTable:
    """A child table that references players_master."""

    table: str
    player_column: str
    conflict_columns: tuple[str, ...] | None = None
    singleton_per_player: bool = False


MERGE_PLANS: tuple[MergePlan, ...] = (
    MergePlan(
        source_rank=10,
        source_name="Mikel Brown Jr.",
        keep_id=5389,
        discard_ids=(5710,),
        canonical_display_name="Mikel Brown Jr.",
        canonical_school="Louisville",
        source_school_raw="Louisville",
        draft_year=2026,
        reason="Keep suffix-canonical row with more mentions and source-matching school.",
    ),
    MergePlan(
        source_rank=13,
        source_name="Labaron Philon",
        keep_id=1709,
        discard_ids=(5572,),
        canonical_display_name="Labaron Philon",
        canonical_school="Alabama",
        source_school_raw="Alabama",
        draft_year=2026,
        reason="Keep non-stub row with combine, metrics, similarity, and status data.",
    ),
    MergePlan(
        source_rank=16,
        source_name="Darius Acuff Jr.",
        keep_id=5384,
        discard_ids=(5681, 6027),
        canonical_display_name="Darius Acuff Jr.",
        canonical_school="Arkansas",
        source_school_raw="Arkansas",
        draft_year=2026,
        reason="Keep suffix-canonical row with reference image and most mentions.",
    ),
    MergePlan(
        source_rank=23,
        source_name="Chris Cenac Jr.",
        keep_id=5387,
        discard_ids=(5791,),
        canonical_display_name="Chris Cenac Jr.",
        canonical_school="Houston",
        source_school_raw="Houston",
        draft_year=2026,
        reason="Keep suffix-canonical row with more mentions; correct draft year to 2026.",
    ),
    MergePlan(
        source_rank=43,
        source_name="Tarris Reed Jr.",
        keep_id=5483,
        discard_ids=(5542, 6037),
        canonical_display_name="Tarris Reed Jr.",
        canonical_school="UConn",
        source_school_raw="Connecticut",
        draft_year=2026,
        reason="Keep suffix-canonical UConn row with most mentions.",
    ),
    MergePlan(
        source_rank=52,
        source_name="Morez Johnson Jr.",
        keep_id=5529,
        discard_ids=(5706, 6038),
        canonical_display_name="Morez Johnson Jr.",
        canonical_school="Michigan",
        source_school_raw="Michigan",
        draft_year=2026,
        reason="Keep suffix-canonical row with most mentions and existing image data.",
    ),
    MergePlan(
        source_rank=61,
        source_name="Kwame Evans Jr.",
        keep_id=5640,
        discard_ids=(5452,),
        canonical_display_name="Kwame Evans Jr.",
        canonical_school="Oregon",
        source_school_raw="Oregon",
        draft_year=2026,
        reason="Keep suffix-canonical row and move richer no-suffix child records into it.",
    ),
    MergePlan(
        source_rank=72,
        source_name="Ja’Kobi Gillespie",
        keep_id=5467,
        discard_ids=(6033,),
        canonical_display_name="Ja’Kobi Gillespie",
        canonical_school="Tennessee",
        source_school_raw="Tennessee",
        draft_year=2026,
        reason="Keep richer Tennessee row and preserve curly-apostrophe variant as canonical.",
    ),
    MergePlan(
        source_rank=78,
        source_name="William Kyle",
        keep_id=5798,
        discard_ids=(5826,),
        canonical_display_name="William Kyle",
        canonical_school="Syracuse",
        source_school_raw="Syracuse",
        draft_year=2026,
        reason="Keep source-name row with existing image data; preserve suffix variant as alias.",
    ),
)


CHILD_TABLES: tuple[ChildTable, ...] = (
    ChildTable("player_content_mentions", "player_id", ("content_type", "content_id")),
    ChildTable("player_college_stats", "player_id", ("season",)),
    ChildTable("player_aliases", "player_id", ("full_name",)),
    ChildTable("player_bio_snapshots", "player_id"),
    ChildTable("player_external_ids", "player_id", ("system", "external_id")),
    ChildTable("player_status", "player_id", singleton_per_player=True),
    ChildTable(
        "player_metric_values",
        "player_id",
        ("snapshot_id", "metric_definition_id"),
    ),
    ChildTable("player_image_assets", "player_id", ("snapshot_id",)),
    ChildTable("pending_image_previews", "player_id"),
    ChildTable("combine_anthro", "player_id", ("season_id",)),
    ChildTable("combine_agility", "player_id", ("season_id",)),
    ChildTable("combine_shooting_results", "player_id", ("season_id",)),
    ChildTable("news_items", "player_id"),
    ChildTable("podcast_episodes", "player_id"),
    ChildTable("player_lifecycle", "player_id", singleton_per_player=True),
)


SIMILARITY_TABLES: tuple[ChildTable, ...] = (
    ChildTable(
        "player_similarity",
        "anchor_player_id",
        ("snapshot_id", "comparison_player_id", "dimension"),
    ),
    ChildTable(
        "player_similarity",
        "comparison_player_id",
        ("snapshot_id", "anchor_player_id", "dimension"),
    ),
)


def _prepare_connection(url: str) -> tuple[str, dict[str, ssl.SSLContext]]:
    """Strip Neon query params that asyncpg does not accept."""
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url.split("://", 1)[1]
    elif url.startswith("postgres://"):
        url = "postgresql+asyncpg://" + url.split("://", 1)[1]

    split = urlsplit(url)
    pairs = parse_qsl(split.query, keep_blank_values=True)
    sslmode = None
    filtered = []
    for key, value in pairs:
        if key == "sslmode":
            sslmode = value
        elif key != "channel_binding":
            filtered.append((key, value))

    cleaned = urlunsplit(split._replace(query=urlencode(filtered))).rstrip("?")
    connect_args: dict[str, ssl.SSLContext] = {}
    if sslmode and sslmode.lower() == "require":
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        connect_args["ssl"] = context
    return cleaned, connect_args


def write_plan_csv(output_date: date) -> Path:
    """Write the reviewed merge plan as a CSV artifact."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"top100_dev_merge_plan_{output_date.isoformat()}.csv"
    fields = [
        "source_rank",
        "source_name",
        "keep_id",
        "discard_ids",
        "canonical_display_name",
        "canonical_school",
        "source_school_raw",
        "draft_year",
        "reason",
    ]
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for plan in MERGE_PLANS:
            writer.writerow(
                {
                    "source_rank": plan.source_rank,
                    "source_name": plan.source_name,
                    "keep_id": plan.keep_id,
                    "discard_ids": "|".join(
                        str(player_id) for player_id in plan.discard_ids
                    ),
                    "canonical_display_name": plan.canonical_display_name,
                    "canonical_school": plan.canonical_school,
                    "source_school_raw": plan.source_school_raw,
                    "draft_year": plan.draft_year,
                    "reason": plan.reason,
                }
            )
    return path


async def _fetch_display_name(conn: Any, player_id: int) -> str | None:
    row = (
        await conn.execute(
            text("SELECT display_name FROM players_master WHERE id = :player_id"),
            {"player_id": player_id},
        )
    ).fetchone()
    return str(row[0]) if row and row[0] else None


async def _count_rows(conn: Any, table: str, column: str, player_id: int) -> int:
    value = (
        await conn.execute(
            text(f"SELECT count(*) FROM {table} WHERE {column} = :player_id"),
            {"player_id": player_id},
        )
    ).scalar()
    return int(value or 0)


async def _delete_similarity_self_links(
    conn: Any,
    *,
    keep_id: int,
    discard_id: int,
    dry_run: bool,
) -> int:
    """Remove similarity rows that would become keep-to-keep self-links."""
    count = (
        await conn.execute(
            text("""
                SELECT count(*)
                FROM player_similarity
                WHERE (anchor_player_id = :discard_id AND comparison_player_id = :keep_id)
                   OR (anchor_player_id = :keep_id AND comparison_player_id = :discard_id)
                   OR (anchor_player_id = :discard_id AND comparison_player_id = :discard_id)
            """),
            {"keep_id": keep_id, "discard_id": discard_id},
        )
    ).scalar()
    total = int(count or 0)
    if total and not dry_run:
        await conn.execute(
            text("""
                DELETE FROM player_similarity
                WHERE (anchor_player_id = :discard_id AND comparison_player_id = :keep_id)
                   OR (anchor_player_id = :keep_id AND comparison_player_id = :discard_id)
                   OR (anchor_player_id = :discard_id AND comparison_player_id = :discard_id)
            """),
            {"keep_id": keep_id, "discard_id": discard_id},
        )
    return total


async def _merge_child_table(
    conn: Any,
    spec: ChildTable,
    *,
    keep_id: int,
    discard_id: int,
    dry_run: bool,
) -> tuple[int, int, int]:
    """Merge one child table and return affected, deleted-conflict, reassigned counts."""
    count = await _count_rows(conn, spec.table, spec.player_column, discard_id)
    if count == 0:
        return 0, 0, 0

    if spec.singleton_per_player:
        keep_count = await _count_rows(conn, spec.table, spec.player_column, keep_id)
        if keep_count:
            if not dry_run:
                await conn.execute(
                    text(
                        f"DELETE FROM {spec.table} "
                        f"WHERE {spec.player_column} = :discard_id"
                    ),
                    {"discard_id": discard_id},
                )
            return count, count, 0
        if not dry_run:
            await conn.execute(
                text(
                    f"UPDATE {spec.table} SET {spec.player_column} = :keep_id "
                    f"WHERE {spec.player_column} = :discard_id"
                ),
                {"keep_id": keep_id, "discard_id": discard_id},
            )
        return count, 0, count

    conflicts = 0
    if spec.conflict_columns:
        conflict_where = " AND ".join(
            f"d.{column} = k.{column}" for column in spec.conflict_columns
        )
        conflicts = int(
            (
                await conn.execute(
                    text(f"""
                        SELECT count(*)
                        FROM {spec.table} d
                        JOIN {spec.table} k
                          ON k.{spec.player_column} = :keep_id
                         AND {conflict_where}
                        WHERE d.{spec.player_column} = :discard_id
                    """),
                    {"keep_id": keep_id, "discard_id": discard_id},
                )
            ).scalar()
            or 0
        )
        if conflicts and not dry_run:
            await conn.execute(
                text(f"""
                    DELETE FROM {spec.table} d
                    USING {spec.table} k
                    WHERE d.{spec.player_column} = :discard_id
                      AND k.{spec.player_column} = :keep_id
                      AND {conflict_where}
                """),
                {"keep_id": keep_id, "discard_id": discard_id},
            )

    reassigned = count - conflicts
    if reassigned and not dry_run:
        await conn.execute(
            text(
                f"UPDATE {spec.table} SET {spec.player_column} = :keep_id "
                f"WHERE {spec.player_column} = :discard_id"
            ),
            {"keep_id": keep_id, "discard_id": discard_id},
        )
    return count, conflicts, reassigned


async def _ensure_alias(
    conn: Any, player_id: int, full_name: str, context: str
) -> None:
    parsed = parse_player_name(full_name)
    await conn.execute(
        text("""
            INSERT INTO player_aliases
                (player_id, full_name, first_name, middle_name, last_name, suffix, context, created_at)
            VALUES
                (:player_id, :full_name, :first_name, :middle_name, :last_name, :suffix, :context, now())
            ON CONFLICT DO NOTHING
        """),
        {
            "player_id": player_id,
            "full_name": full_name,
            "first_name": parsed.first_name or None,
            "middle_name": parsed.middle_name,
            "last_name": parsed.last_name,
            "suffix": parsed.suffix,
            "context": context,
        },
    )


async def _update_keep_player(conn: Any, plan: MergePlan, dry_run: bool) -> None:
    parsed = parse_player_name(plan.canonical_display_name)
    if dry_run:
        print(
            "  WOULD UPDATE keep player fields: "
            f"display_name={plan.canonical_display_name!r}, "
            f"school={plan.canonical_school!r}, school_raw={plan.source_school_raw!r}, "
            f"draft_year={plan.draft_year}"
        )
        return

    await conn.execute(
        text("""
            UPDATE players_master
            SET display_name = :display_name,
                first_name = :first_name,
                middle_name = :middle_name,
                last_name = :last_name,
                suffix = :suffix,
                school = :school,
                school_raw = :school_raw,
                draft_year = :draft_year,
                updated_at = :updated_at
            WHERE id = :player_id
        """),
        {
            "player_id": plan.keep_id,
            "display_name": plan.canonical_display_name,
            "first_name": parsed.first_name or None,
            "middle_name": parsed.middle_name,
            "last_name": parsed.last_name,
            "suffix": parsed.suffix,
            "school": plan.canonical_school,
            "school_raw": plan.source_school_raw,
            "draft_year": plan.draft_year,
            "updated_at": datetime.now(UTC).replace(tzinfo=None),
        },
    )
    await _ensure_alias(
        conn,
        plan.keep_id,
        plan.canonical_display_name,
        "top100_dedup_canonical",
    )


async def merge_plan(conn: Any, plan: MergePlan, dry_run: bool) -> None:
    """Apply one reviewed merge group."""
    keep_name = await _fetch_display_name(conn, plan.keep_id)
    if keep_name is None:
        print(f"\nSKIP {plan.source_name}: keep id {plan.keep_id} not found")
        return

    print(
        f"\n{'[DRY RUN] ' if dry_run else ''}{plan.source_name}: "
        f"keep {plan.keep_id} ({keep_name}) <- discard {plan.discard_ids}"
    )
    print(f"  reason: {plan.reason}")

    for discard_id in plan.discard_ids:
        discard_name = await _fetch_display_name(conn, discard_id)
        if discard_name is None:
            print(f"  discard {discard_id}: already absent, skipping")
            continue

        print(f"  discard {discard_id} ({discard_name})")
        self_link_deletes = await _delete_similarity_self_links(
            conn,
            keep_id=plan.keep_id,
            discard_id=discard_id,
            dry_run=dry_run,
        )
        if self_link_deletes:
            print(
                f"    player_similarity self/conflicting keep links: delete {self_link_deletes}"
            )

        for spec in (*CHILD_TABLES, *SIMILARITY_TABLES):
            affected, deleted, reassigned = await _merge_child_table(
                conn,
                spec,
                keep_id=plan.keep_id,
                discard_id=discard_id,
                dry_run=dry_run,
            )
            if affected:
                print(
                    f"    {spec.table}.{spec.player_column}: "
                    f"affected={affected}, delete_conflicts={deleted}, reassign={reassigned}"
                )

        if dry_run:
            print(f"    WOULD ADD alias {discard_name!r} -> {plan.keep_id}")
            print(f"    WOULD DELETE player {discard_id}")
        else:
            await _ensure_alias(
                conn, plan.keep_id, discard_name, "top100_dedup_discard"
            )
            await conn.execute(
                text("DELETE FROM players_master WHERE id = :discard_id"),
                {"discard_id": discard_id},
            )
            print(f"    deleted player {discard_id}")

    await _update_keep_player(conn, plan, dry_run)


async def run(dry_run: bool, output_date: date) -> None:
    """Run the reviewed Top 100 dev merge plan."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        sys.exit(1)

    plan_path = write_plan_csv(output_date)
    print(f"Merge plan artifact: {plan_path}")
    print(f"Mode: {'DRY RUN' if dry_run else 'EXECUTE'}")

    cleaned_url, connect_args = _prepare_connection(database_url)
    engine = create_async_engine(cleaned_url, echo=False, connect_args=connect_args)

    async with engine.begin() as conn:
        for plan in MERGE_PLANS:
            await merge_plan(conn, plan, dry_run=dry_run)

        if dry_run:
            await conn.rollback()
            print("\nDry run complete; transaction rolled back.")
        else:
            print("\nMerge execution committed.")

    await engine.dispose()


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description="Apply reviewed Top 100 player merges")
    parser.add_argument("--execute", action="store_true", help="Apply changes")
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        default=date.today(),
        help="Artifact date in YYYY-MM-DD format",
    )
    return parser.parse_args()


def main() -> None:
    """CLI entrypoint."""
    args = parse_args()
    asyncio.run(run(dry_run=not args.execute, output_date=args.date))


if __name__ == "__main__":
    main()
