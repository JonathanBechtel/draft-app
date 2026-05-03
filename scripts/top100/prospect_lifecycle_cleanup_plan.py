"""Generate a read-only lifecycle cleanup plan for prospect flags.

The output classifies production lifecycle rows whose `is_draft_prospect` and
`expected_draft_year` values need cleanup before prospect filters can be trusted.

Usage:
    conda run -n draftguru python scripts/top100/prospect_lifecycle_cleanup_plan.py \
        --date 2026-04-27 --environment prod --database-url "$DATABASE_URL"
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import sys
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.canonical_resolution_service import normalize_player_name  # noqa: E402
from scripts.top100.refresh import (  # noqa: E402
    OUTPUT_DIR,
    TOP100_ROWS,
    _prepare_connection,
)


CURRENT_TOP100_DRAFT_YEAR = 2026
CURRENT_PROSPECT_START_YEAR = 2025
CURRENT_PROSPECT_END_YEAR = 2028
NON_PROSPECT_LIFECYCLE_STAGES = {
    "DRAFTED_NOT_IN_NBA",
    "NBA_ACTIVE",
    "INACTIVE_FORMER",
}
NON_PROSPECT_DRAFT_STATUSES = {
    "DRAFTED",
    "UNDRAFTED",
}


@dataclass(frozen=True, slots=True)
class CleanupPlanRow:
    """One lifecycle cleanup recommendation."""

    player_id: int
    display_name: str
    normalized_name: str
    is_top100_source: bool
    top100_rank: int | None
    draft_year: int | None
    expected_draft_year: int | None
    draft_status: str
    lifecycle_stage: str
    competition_context: str
    current_affiliation_name: str
    current_affiliation_type: str
    is_draft_prospect: bool | None
    proposed_is_draft_prospect: bool | None
    proposed_expected_draft_year: int | None
    classification: str
    reason: str
    confidence: str


def _top100_lookup() -> dict[str, int]:
    """Return normalized Top 100 names keyed to source rank."""
    return {
        normalize_player_name(row.source_name): row.source_rank for row in TOP100_ROWS
    }


def _enum_value(value: Any) -> str:
    """Convert DB enum values to strings for comparisons and CSVs."""
    return "" if value is None else str(value)


def _classify_row(
    row: dict[str, Any], top100_by_name: dict[str, int]
) -> CleanupPlanRow | None:
    """Return a cleanup recommendation for rows with lifecycle issues."""
    player_id = int(row["player_id"])
    display_name = str(row["display_name"] or "")
    normalized_name = normalize_player_name(display_name)
    top100_rank = top100_by_name.get(normalized_name)
    is_top100_source = top100_rank is not None
    draft_year = row["draft_year"]
    expected_draft_year = row["expected_draft_year"]
    lifecycle_stage = _enum_value(row["lifecycle_stage"])
    draft_status = _enum_value(row["draft_status"])
    is_draft_prospect = row["is_draft_prospect"]

    if is_top100_source:
        proposed_expected_year = expected_draft_year or CURRENT_TOP100_DRAFT_YEAR
        if (
            is_draft_prospect is True
            and expected_draft_year == CURRENT_TOP100_DRAFT_YEAR
        ):
            return None
        return CleanupPlanRow(
            player_id=player_id,
            display_name=display_name,
            normalized_name=normalized_name,
            is_top100_source=True,
            top100_rank=top100_rank,
            draft_year=draft_year,
            expected_draft_year=expected_draft_year,
            draft_status=draft_status,
            lifecycle_stage=lifecycle_stage,
            competition_context=_enum_value(row["competition_context"]),
            current_affiliation_name=str(row["current_affiliation_name"] or ""),
            current_affiliation_type=_enum_value(row["current_affiliation_type"]),
            is_draft_prospect=is_draft_prospect,
            proposed_is_draft_prospect=True,
            proposed_expected_draft_year=proposed_expected_year,
            classification="fix_now",
            reason="frozen_top100_source_row_should_be_current_2026_prospect",
            confidence="high",
        )

    if lifecycle_stage in NON_PROSPECT_LIFECYCLE_STAGES and is_draft_prospect is True:
        return CleanupPlanRow(
            player_id=player_id,
            display_name=display_name,
            normalized_name=normalized_name,
            is_top100_source=False,
            top100_rank=None,
            draft_year=draft_year,
            expected_draft_year=expected_draft_year,
            draft_status=draft_status,
            lifecycle_stage=lifecycle_stage,
            competition_context=_enum_value(row["competition_context"]),
            current_affiliation_name=str(row["current_affiliation_name"] or ""),
            current_affiliation_type=_enum_value(row["current_affiliation_type"]),
            is_draft_prospect=is_draft_prospect,
            proposed_is_draft_prospect=False,
            proposed_expected_draft_year=expected_draft_year,
            classification="fix_now",
            reason="non_prospect_lifecycle_stage_marked_as_draft_prospect",
            confidence="high",
        )

    if draft_status in NON_PROSPECT_DRAFT_STATUSES and is_draft_prospect is True:
        return CleanupPlanRow(
            player_id=player_id,
            display_name=display_name,
            normalized_name=normalized_name,
            is_top100_source=False,
            top100_rank=None,
            draft_year=draft_year,
            expected_draft_year=expected_draft_year,
            draft_status=draft_status,
            lifecycle_stage=lifecycle_stage,
            competition_context=_enum_value(row["competition_context"]),
            current_affiliation_name=str(row["current_affiliation_name"] or ""),
            current_affiliation_type=_enum_value(row["current_affiliation_type"]),
            is_draft_prospect=is_draft_prospect,
            proposed_is_draft_prospect=False,
            proposed_expected_draft_year=expected_draft_year,
            classification="fix_now",
            reason="non_prospect_draft_status_marked_as_draft_prospect",
            confidence="high",
        )

    if (
        is_draft_prospect is True
        and draft_year is not None
        and int(draft_year) < CURRENT_PROSPECT_START_YEAR
    ):
        return CleanupPlanRow(
            player_id=player_id,
            display_name=display_name,
            normalized_name=normalized_name,
            is_top100_source=False,
            top100_rank=None,
            draft_year=draft_year,
            expected_draft_year=expected_draft_year,
            draft_status=draft_status,
            lifecycle_stage=lifecycle_stage,
            competition_context=_enum_value(row["competition_context"]),
            current_affiliation_name=str(row["current_affiliation_name"] or ""),
            current_affiliation_type=_enum_value(row["current_affiliation_type"]),
            is_draft_prospect=is_draft_prospect,
            proposed_is_draft_prospect=False,
            proposed_expected_draft_year=expected_draft_year,
            classification="backlog",
            reason="old_draft_year_still_marked_as_prospect",
            confidence="medium",
        )

    if is_draft_prospect is True and expected_draft_year is None:
        return CleanupPlanRow(
            player_id=player_id,
            display_name=display_name,
            normalized_name=normalized_name,
            is_top100_source=False,
            top100_rank=None,
            draft_year=draft_year,
            expected_draft_year=expected_draft_year,
            draft_status=draft_status,
            lifecycle_stage=lifecycle_stage,
            competition_context=_enum_value(row["competition_context"]),
            current_affiliation_name=str(row["current_affiliation_name"] or ""),
            current_affiliation_type=_enum_value(row["current_affiliation_type"]),
            is_draft_prospect=is_draft_prospect,
            proposed_is_draft_prospect=True,
            proposed_expected_draft_year=None,
            classification="manual_review",
            reason="current_draft_prospect_missing_expected_draft_year",
            confidence="low",
        )

    if (
        is_draft_prospect is True
        and expected_draft_year is not None
        and int(expected_draft_year) < CURRENT_PROSPECT_START_YEAR
    ):
        return CleanupPlanRow(
            player_id=player_id,
            display_name=display_name,
            normalized_name=normalized_name,
            is_top100_source=False,
            top100_rank=None,
            draft_year=draft_year,
            expected_draft_year=expected_draft_year,
            draft_status=draft_status,
            lifecycle_stage=lifecycle_stage,
            competition_context=_enum_value(row["competition_context"]),
            current_affiliation_name=str(row["current_affiliation_name"] or ""),
            current_affiliation_type=_enum_value(row["current_affiliation_type"]),
            is_draft_prospect=is_draft_prospect,
            proposed_is_draft_prospect=False,
            proposed_expected_draft_year=expected_draft_year,
            classification="backlog",
            reason="old_expected_draft_year_still_marked_as_prospect",
            confidence="medium",
        )

    return None


async def _fetch_rows(database_url: str) -> list[dict[str, Any]]:
    """Fetch lifecycle rows in or near prospect scope."""
    cleaned_url, connect_args = _prepare_connection(database_url)
    engine = create_async_engine(cleaned_url, echo=False, connect_args=connect_args)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    """
                    SELECT
                        pm.id AS player_id,
                        pm.display_name,
                        pm.draft_year,
                        pm.is_stub,
                        pl.expected_draft_year,
                        pl.draft_status,
                        pl.lifecycle_stage,
                        pl.competition_context,
                        pl.current_affiliation_name,
                        pl.current_affiliation_type,
                        pl.is_draft_prospect
                    FROM players_master pm
                    LEFT JOIN player_lifecycle pl ON pl.player_id = pm.id
                    WHERE pm.draft_year BETWEEN :start_year AND :end_year
                       OR pl.expected_draft_year BETWEEN :start_year AND :end_year
                       OR pl.is_draft_prospect IS true
                       OR pm.is_stub IS true
                       OR pm.display_name = ANY(:top100_names)
                    ORDER BY pm.id
                    """
                ),
                {
                    "start_year": CURRENT_PROSPECT_START_YEAR,
                    "end_year": CURRENT_PROSPECT_END_YEAR,
                    "top100_names": [row.source_name for row in TOP100_ROWS],
                },
            )
            return [dict(row) for row in result.mappings()]
    finally:
        await engine.dispose()


def write_csv(path: Path, rows: list[CleanupPlanRow]) -> None:
    """Write the cleanup plan CSV."""
    with path.open("w", newline="", encoding="utf-8") as file:
        fieldnames = list(
            asdict(
                CleanupPlanRow(
                    0,
                    "",
                    "",
                    False,
                    None,
                    None,
                    None,
                    "",
                    "",
                    "",
                    "",
                    "",
                    None,
                    None,
                    None,
                    "",
                    "",
                    "",
                )
            )
        )
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(asdict(row) for row in rows)


async def generate_cleanup_plan(
    output_date: date,
    environment: str,
    database_url: str,
) -> Path:
    """Generate the read-only cleanup plan artifact."""
    rows = await _fetch_rows(database_url)
    top100_by_name = _top100_lookup()
    plan_rows = [
        plan_row
        for row in rows
        if (plan_row := _classify_row(row, top100_by_name)) is not None
    ]
    plan_rows.sort(
        key=lambda row: (
            {"fix_now": 0, "manual_review": 1, "backlog": 2}.get(row.classification, 9),
            row.top100_rank or 999,
            row.player_id,
        )
    )
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = (
        OUTPUT_DIR
        / f"{environment}_prospect_lifecycle_cleanup_plan_{output_date.isoformat()}.csv"
    )
    write_csv(path, plan_rows)
    return path


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Generate prospect lifecycle cleanup plan"
    )
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        default=date.today(),
        help="Artifact date in YYYY-MM-DD format",
    )
    parser.add_argument(
        "--environment",
        default="prod",
        help="Artifact prefix/environment label, e.g. dev, stage, prod",
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
        path = asyncio.run(
            generate_cleanup_plan(args.date, args.environment, args.database_url)
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    print(path)


if __name__ == "__main__":
    main()
