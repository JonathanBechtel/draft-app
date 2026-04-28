"""Merge the five production duplicate player groups from Session #5 QA.

This is a narrowly scoped production data cleanup script. It preserves discarded
display names as aliases, reassigns child rows to the kept player, deletes
child rows that would conflict with an equivalent keep-player row, and then
deletes the duplicate player record.

Usage:
    conda run -n draftguru python scripts/prod_duplicate_player_cleanup.py --dry-run
    conda run -n draftguru python scripts/prod_duplicate_player_cleanup.py --execute
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.player_mention_service import parse_player_name  # noqa: E402
from scripts.top100_merge_players import (  # noqa: E402
    CHILD_TABLES,
    SIMILARITY_TABLES,
    _delete_similarity_self_links,
    _ensure_alias,
    _fetch_display_name,
    _merge_child_table,
)
from scripts.top100_refresh import OUTPUT_DIR, _prepare_connection  # noqa: E402


@dataclass(frozen=True, slots=True)
class ProdMergePlan:
    """One reviewed prod duplicate merge group."""

    group_name: str
    keep_id: int
    discard_ids: tuple[int, ...]
    canonical_display_name: str
    canonical_school: str | None
    canonical_school_raw: str | None
    canonical_draft_year: int | None
    reason: str


@dataclass(frozen=True, slots=True)
class MergeLogRow:
    """One per-discard merge result row."""

    group_name: str
    keep_id: int
    keep_name: str
    discard_id: int
    discard_name: str
    child_rows_reassigned: int
    conflict_rows_deleted: int
    self_similarity_rows_deleted: int
    action: str


MERGE_PLANS: tuple[ProdMergePlan, ...] = (
    ProdMergePlan(
        group_name="Hugo Gonzalez",
        keep_id=1680,
        discard_ids=(6126,),
        canonical_display_name="Hugo González",
        canonical_school=None,
        canonical_school_raw=None,
        canonical_draft_year=2025,
        reason="Keep authoritative drafted/NBA record with external IDs; merge Real Madrid prospect stub into it.",
    ),
    ProdMergePlan(
        group_name="Jordan Smith",
        keep_id=6359,
        discard_ids=(6175,),
        canonical_display_name="Jordan Smith Jr.",
        canonical_school="Arkansas",
        canonical_school_raw="Arkansas",
        canonical_draft_year=2027,
        reason="Keep row with stats/current 2027 prospect metadata; preserve suffix variant as canonical display.",
    ),
    ProdMergePlan(
        group_name="RJ Luis",
        keep_id=1692,
        discard_ids=(6162,),
        canonical_display_name="RJ Luis Jr.",
        canonical_school="St. John's",
        canonical_school_raw="St. John's",
        canonical_draft_year=2025,
        reason="Keep drafted/non-NBA canonical suffix row; merge college stats stub into it.",
    ),
    ProdMergePlan(
        group_name="TJ Power",
        keep_id=6340,
        discard_ids=(6341,),
        canonical_display_name="TJ Power",
        canonical_school="University of Pennsylvania",
        canonical_school_raw="University of Pennsylvania",
        canonical_draft_year=2027,
        reason="Keep lower-id row and preserve punctuation variant as alias.",
    ),
    ProdMergePlan(
        group_name="VJ Edgecombe",
        keep_id=1673,
        discard_ids=(6124,),
        canonical_display_name="VJ Edgecombe",
        canonical_school="Baylor",
        canonical_school_raw="Baylor",
        canonical_draft_year=2025,
        reason="Keep authoritative drafted/NBA record with external IDs and richer mentions; merge prospect stub into it.",
    ),
)


def _write_plan(path: Path) -> None:
    """Write the reviewed prod merge plan."""
    with path.open("w", newline="", encoding="utf-8") as file:
        fieldnames = list(asdict(ProdMergePlan("", 0, (), "", None, None, None, "")))
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for plan in MERGE_PLANS:
            row = asdict(plan)
            row["discard_ids"] = "|".join(
                str(player_id) for player_id in plan.discard_ids
            )
            writer.writerow(row)


def _write_log(path: Path, rows: list[MergeLogRow]) -> None:
    """Write merge execution log rows."""
    with path.open("w", newline="", encoding="utf-8") as file:
        fieldnames = list(asdict(MergeLogRow("", 0, "", 0, "", 0, 0, 0, "")))
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(asdict(row) for row in rows)


async def _update_keep_player(
    conn: Any,
    plan: ProdMergePlan,
    *,
    dry_run: bool,
) -> None:
    """Apply conservative canonical fields to the keep player."""
    parsed = parse_player_name(plan.canonical_display_name)
    if dry_run:
        return
    await conn.execute(
        text(
            """
            UPDATE players_master
            SET display_name = :display_name,
                first_name = :first_name,
                middle_name = :middle_name,
                last_name = :last_name,
                suffix = :suffix,
                school = COALESCE(:school, school),
                school_raw = COALESCE(:school_raw, school_raw),
                draft_year = COALESCE(:draft_year, draft_year),
                is_stub = false,
                updated_at = :updated_at
            WHERE id = :player_id
            """
        ),
        {
            "player_id": plan.keep_id,
            "display_name": plan.canonical_display_name,
            "first_name": parsed.first_name or None,
            "middle_name": parsed.middle_name,
            "last_name": parsed.last_name,
            "suffix": parsed.suffix,
            "school": plan.canonical_school,
            "school_raw": plan.canonical_school_raw,
            "draft_year": plan.canonical_draft_year,
            "updated_at": datetime.now(UTC).replace(tzinfo=None),
        },
    )
    await _ensure_alias(
        conn,
        plan.keep_id,
        plan.canonical_display_name,
        "prod_duplicate_cleanup_canonical",
    )


async def _merge_one_discard(
    conn: Any,
    plan: ProdMergePlan,
    discard_id: int,
    *,
    dry_run: bool,
) -> MergeLogRow:
    """Merge one discard player into the keep player."""
    keep_name = await _fetch_display_name(conn, plan.keep_id)
    discard_name = await _fetch_display_name(conn, discard_id)
    if keep_name is None:
        return MergeLogRow(
            plan.group_name,
            plan.keep_id,
            "",
            discard_id,
            discard_name or "",
            0,
            0,
            0,
            "skip_keep_missing",
        )
    if discard_name is None:
        return MergeLogRow(
            plan.group_name,
            plan.keep_id,
            keep_name,
            discard_id,
            "",
            0,
            0,
            0,
            "skip_discard_missing",
        )

    self_link_deletes = await _delete_similarity_self_links(
        conn,
        keep_id=plan.keep_id,
        discard_id=discard_id,
        dry_run=dry_run,
    )
    reassigned_total = 0
    conflict_total = 0
    for spec in (*CHILD_TABLES, *SIMILARITY_TABLES):
        _affected, conflicts, reassigned = await _merge_child_table(
            conn,
            spec,
            keep_id=plan.keep_id,
            discard_id=discard_id,
            dry_run=dry_run,
        )
        reassigned_total += reassigned
        conflict_total += conflicts

    if not dry_run:
        await _ensure_alias(
            conn,
            plan.keep_id,
            discard_name,
            "prod_duplicate_cleanup_discard",
        )
        await conn.execute(
            text("DELETE FROM players_master WHERE id = :discard_id"),
            {"discard_id": discard_id},
        )

    return MergeLogRow(
        group_name=plan.group_name,
        keep_id=plan.keep_id,
        keep_name=keep_name,
        discard_id=discard_id,
        discard_name=discard_name,
        child_rows_reassigned=reassigned_total,
        conflict_rows_deleted=conflict_total,
        self_similarity_rows_deleted=self_link_deletes,
        action="would_merge" if dry_run else "merged",
    )


async def run_cleanup(
    *,
    database_url: str,
    output_date: date,
    execute: bool,
) -> tuple[Path, Path, list[MergeLogRow]]:
    """Run dry-run or execute mode for the prod duplicate merges."""
    mode = "execute" if execute else "dry_run"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    plan_path = OUTPUT_DIR / f"prod_duplicate_merge_plan_{output_date.isoformat()}.csv"
    log_path = OUTPUT_DIR / f"prod_duplicate_merge_{mode}_{output_date.isoformat()}.csv"
    _write_plan(plan_path)

    cleaned_url, connect_args = _prepare_connection(database_url)
    engine = create_async_engine(cleaned_url, echo=False, connect_args=connect_args)
    log_rows: list[MergeLogRow] = []
    try:
        async with engine.begin() as conn:
            for plan in MERGE_PLANS:
                for discard_id in plan.discard_ids:
                    log_rows.append(
                        await _merge_one_discard(
                            conn,
                            plan,
                            discard_id,
                            dry_run=not execute,
                        )
                    )
                await _update_keep_player(conn, plan, dry_run=not execute)
            if not execute:
                await conn.rollback()
    finally:
        await engine.dispose()

    _write_log(log_path, log_rows)
    return plan_path, log_path, log_rows


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description="Merge prod duplicate players")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Write plan/log only")
    mode.add_argument("--execute", action="store_true", help="Apply merges")
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        default=date.today(),
        help="Artifact date in YYYY-MM-DD format",
    )
    parser.add_argument(
        "--database-url",
        default=os.getenv("DATABASE_URL"),
        help="Database URL; defaults to DATABASE_URL from environment/.env",
    )
    return parser.parse_args()


def main() -> None:
    """CLI entrypoint."""
    load_dotenv()
    args = parse_args()
    if not args.database_url:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        sys.exit(1)
    try:
        plan_path, log_path, rows = asyncio.run(
            run_cleanup(
                database_url=args.database_url,
                output_date=args.date,
                execute=args.execute,
            )
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    print(plan_path)
    print(log_path)
    print(f"merge_rows={len(rows)}")


if __name__ == "__main__":
    main()
