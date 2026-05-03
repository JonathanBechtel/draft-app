"""Clean high-confidence production prospect lifecycle flag issues.

This script targets Session #5 lifecycle QA findings where production rows were
marked as draft prospects by the original migration backfill but lack an
expected draft year. It writes a manifest in dry-run and execute modes.

Usage:
    conda run -n draftguru python scripts/top100/prod_lifecycle_expected_year_cleanup.py --dry-run
    conda run -n draftguru python scripts/top100/prod_lifecycle_expected_year_cleanup.py --execute
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

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.top100.refresh import OUTPUT_DIR, _prepare_connection  # noqa: E402


CURRENT_DRAFT_YEAR = 2026
HISTORICAL_CUTOFF = date(1990, 1, 1)
CURRENT_PLAYER_OVERRIDES: dict[int, tuple[bool, int | None, str]] = {
    6123: (
        True,
        CURRENT_DRAFT_YEAR,
        "Bryce James is a 2025-26 Arizona men's college player and draft-eligible in the current cycle.",
    ),
    6169: (
        True,
        CURRENT_DRAFT_YEAR,
        "Amare Bynum is a 2025-26 Ohio State men's college player and draft-eligible in the current cycle.",
    ),
    6316: (
        True,
        CURRENT_DRAFT_YEAR,
        "Adam Olsen is a 2025-26 South Alabama men's college player and draft-eligible in the current cycle.",
    ),
    6357: (
        False,
        None,
        "Saniyah Hall is a women's college basketball recruit, outside NBA DraftGuru prospect scope.",
    ),
}


@dataclass(frozen=True, slots=True)
class LifecycleCleanupRow:
    """One lifecycle cleanup decision."""

    player_id: int
    display_name: str
    birthdate: str
    draft_year: int | None
    school: str
    lifecycle_stage: str
    draft_status: str
    source: str
    has_bbr_external_id: bool
    old_is_draft_prospect: bool | None
    old_expected_draft_year: int | None
    new_is_draft_prospect: bool | None
    new_expected_draft_year: int | None
    action: str
    rationale: str


def _csv_fieldnames() -> list[str]:
    """Return CSV fieldnames matching the cleanup dataclass."""
    return list(
        asdict(
            LifecycleCleanupRow(
                player_id=0,
                display_name="",
                birthdate="",
                draft_year=None,
                school="",
                lifecycle_stage="",
                draft_status="",
                source="",
                has_bbr_external_id=False,
                old_is_draft_prospect=None,
                old_expected_draft_year=None,
                new_is_draft_prospect=None,
                new_expected_draft_year=None,
                action="",
                rationale="",
            )
        )
    )


def _row_birthdate(row: dict[str, Any]) -> date | None:
    """Return a row birthdate as a date object."""
    value = row["birthdate"]
    if value is None or isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _classify_candidate(row: dict[str, Any]) -> LifecycleCleanupRow | None:
    """Classify one missing-expected-year prospect row."""
    player_id = int(row["player_id"])
    old_is_draft_prospect = row["is_draft_prospect"]
    old_expected_year = row["expected_draft_year"]
    has_bbr = bool(row["has_bbr_external_id"])
    birthdate = _row_birthdate(row)

    if player_id in CURRENT_PLAYER_OVERRIDES:
        new_is_prospect, new_expected_year, rationale = CURRENT_PLAYER_OVERRIDES[
            player_id
        ]
        action = (
            "set_expected_draft_year"
            if new_is_prospect
            else "set_is_draft_prospect_false"
        )
    elif (
        row["source"] == "migration_backfill"
        and has_bbr
        and (birthdate is None or birthdate < HISTORICAL_CUTOFF)
    ):
        new_is_prospect = False
        new_expected_year = old_expected_year
        action = "set_is_draft_prospect_false"
        rationale = "Historical Basketball Reference migration-backfill row lacks a current expected draft year."
    else:
        return None

    return LifecycleCleanupRow(
        player_id=player_id,
        display_name=str(row["display_name"] or ""),
        birthdate="" if birthdate is None else birthdate.isoformat(),
        draft_year=row["draft_year"],
        school=str(row["school"] or ""),
        lifecycle_stage=str(row["lifecycle_stage"] or ""),
        draft_status=str(row["draft_status"] or ""),
        source=str(row["source"] or ""),
        has_bbr_external_id=has_bbr,
        old_is_draft_prospect=old_is_draft_prospect,
        old_expected_draft_year=old_expected_year,
        new_is_draft_prospect=new_is_prospect,
        new_expected_draft_year=new_expected_year,
        action=action,
        rationale=rationale,
    )


async def _fetch_candidates(conn: Any) -> list[dict[str, Any]]:
    """Fetch production lifecycle rows missing expected draft years."""
    result = await conn.execute(
        text(
            """
            SELECT
                pm.id AS player_id,
                pm.display_name,
                pm.birthdate,
                pm.draft_year,
                COALESCE(pm.school, pm.school_raw, pl.current_affiliation_name, '') AS school,
                pl.lifecycle_stage,
                pl.draft_status,
                pl.source,
                pl.is_draft_prospect,
                pl.expected_draft_year,
                EXISTS (
                    SELECT 1
                    FROM player_external_ids pei
                    WHERE pei.player_id = pm.id
                      AND pei.system = 'bbr'
                ) AS has_bbr_external_id
            FROM players_master pm
            JOIN player_lifecycle pl ON pl.player_id = pm.id
            WHERE pl.is_draft_prospect IS true
              AND pl.expected_draft_year IS NULL
            ORDER BY pm.id
            """
        )
    )
    return [dict(row) for row in result.mappings()]


def _write_manifest(path: Path, rows: list[LifecycleCleanupRow]) -> None:
    """Write cleanup decisions to CSV."""
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=_csv_fieldnames())
        writer.writeheader()
        writer.writerows(asdict(row) for row in rows)


async def cleanup_lifecycle_expected_years(
    *,
    database_url: str,
    output_date: date,
    execute: bool,
) -> tuple[Path, Path, list[LifecycleCleanupRow], int]:
    """Generate a manifest and optionally apply lifecycle cleanup updates."""
    cleaned_url, connect_args = _prepare_connection(database_url)
    engine = create_async_engine(cleaned_url, echo=False, connect_args=connect_args)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    mode = "execute" if execute else "dry_run"
    manifest_path = (
        OUTPUT_DIR
        / f"prod_lifecycle_expected_year_cleanup_{mode}_{output_date.isoformat()}.csv"
    )
    log_path = (
        OUTPUT_DIR
        / f"prod_lifecycle_expected_year_cleanup_{mode}_{output_date.isoformat()}.log"
    )
    unmatched_count = 0
    now = datetime.now(UTC).replace(tzinfo=None)

    try:
        async with engine.begin() as conn:
            candidates = await _fetch_candidates(conn)
            cleanup_rows = [
                cleanup_row
                for row in candidates
                if (cleanup_row := _classify_candidate(row)) is not None
            ]
            unmatched_count = len(candidates) - len(cleanup_rows)
            _write_manifest(manifest_path, cleanup_rows)

            if execute and cleanup_rows:
                for cleanup_row in cleanup_rows:
                    await conn.execute(
                        text(
                            """
                            UPDATE player_lifecycle
                            SET is_draft_prospect = :is_draft_prospect,
                                expected_draft_year = :expected_draft_year,
                                updated_at = :updated_at
                            WHERE player_id = :player_id
                            """
                        ),
                        {
                            "player_id": cleanup_row.player_id,
                            "is_draft_prospect": cleanup_row.new_is_draft_prospect,
                            "expected_draft_year": cleanup_row.new_expected_draft_year,
                            "updated_at": now,
                        },
                    )
    finally:
        await engine.dispose()

    action_counts: dict[str, int] = {}
    for row in cleanup_rows:
        action_counts[row.action] = action_counts.get(row.action, 0) + 1
    lines = [
        f"mode={mode}",
        f"executed_at={datetime.now(UTC).isoformat()}",
        f"manifest={manifest_path}",
        f"cleanup_rows={len(cleanup_rows)}",
        f"unmatched_rows={unmatched_count}",
        *[f"{action}={count}" for action, count in sorted(action_counts.items())],
        "player_ids=" + "|".join(str(row.player_id) for row in cleanup_rows),
    ]
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return manifest_path, log_path, cleanup_rows, unmatched_count


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Clean prod lifecycle expected-year gaps"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Write manifest only")
    mode.add_argument("--execute", action="store_true", help="Apply cleanup updates")
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
        manifest_path, log_path, rows, unmatched_count = asyncio.run(
            cleanup_lifecycle_expected_years(
                database_url=args.database_url,
                output_date=args.date,
                execute=args.execute,
            )
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    print(manifest_path)
    print(log_path)
    print(f"cleanup_rows={len(rows)}")
    print(f"unmatched_rows={unmatched_count}")


if __name__ == "__main__":
    main()
