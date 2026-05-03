"""Generate a combined read-only cleanup plan for Session #5 prod blockers.

The plan intentionally does not mutate production. It consolidates duplicate
identity, bad image URL, lifecycle, and affiliation findings into one
row-oriented CSV for review before any production write script is considered.

Usage:
    conda run -n draftguru python scripts/top100/prod_cleanup_plan.py \
        --date 2026-04-27 --database-url "$DATABASE_URL"
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
import re

from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.canonical_resolution_service import (  # noqa: E402
    load_college_school_names,
    load_school_mapping,
    normalize_player_name,
    resolve_affiliation,
)
from scripts.top100.prospect_integrity_audit import _bad_url_reason  # noqa: E402
from scripts.top100.prospect_lifecycle_cleanup_plan import (  # noqa: E402
    generate_cleanup_plan,
)
from scripts.top100.refresh import OUTPUT_DIR, TOP100_ROWS, _prepare_connection  # noqa: E402


PROSPECT_START_YEAR = 2025
PROSPECT_END_YEAR = 2028
NBA_TEAM_NAMES = {
    "Atlanta Hawks",
    "Boston Celtics",
    "Brooklyn Nets",
    "Charlotte Hornets",
    "Chicago Bulls",
    "Cleveland Cavaliers",
    "Dallas Mavericks",
    "Denver Nuggets",
    "Detroit Pistons",
    "Golden State Warriors",
    "Houston Rockets",
    "Indiana Pacers",
    "LA Clippers",
    "Los Angeles Clippers",
    "Los Angeles Lakers",
    "Memphis Grizzlies",
    "Miami Heat",
    "Milwaukee Bucks",
    "Minnesota Timberwolves",
    "New Orleans Pelicans",
    "New York Knicks",
    "Oklahoma City Thunder",
    "Orlando Magic",
    "Philadelphia 76ers",
    "Phoenix Suns",
    "Portland Trail Blazers",
    "Sacramento Kings",
    "San Antonio Spurs",
    "Toronto Raptors",
    "Utah Jazz",
    "Washington Wizards",
}


@dataclass(frozen=True, slots=True)
class CleanupAction:
    """One proposed prod cleanup action."""

    category: str
    action_type: str
    decision: str
    confidence: str
    player_id: int | None
    record_id: int | None
    table_name: str
    field_name: str
    current_value: str
    proposed_value: str
    related_player_ids: str
    source_check: str
    reason: str
    review_note: str


async def _fetch_all(
    conn: Any,
    sql: str,
    params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Execute a read-only query and return mapping rows."""
    result = await conn.execute(text(sql), params or {})
    return [dict(row) for row in result.mappings()]


def _top100_names() -> set[str]:
    """Return normalized frozen Top 100 names."""
    return {normalize_player_name(row.source_name) for row in TOP100_ROWS}


def _compact(value: Any) -> str:
    """Format values for CSV cells."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _clean_affiliation(value: str) -> str:
    """Normalize affiliation punctuation without changing source meaning."""
    return re.sub(r"^[\s:;,-]+", "", value).strip()


def _action(
    *,
    category: str,
    action_type: str,
    decision: str,
    confidence: str,
    player_id: int | None,
    record_id: int | None = None,
    table_name: str = "",
    field_name: str = "",
    current_value: str = "",
    proposed_value: str = "",
    related_player_ids: list[int] | None = None,
    source_check: str,
    reason: str,
    review_note: str,
) -> CleanupAction:
    """Build a stable cleanup action row."""
    return CleanupAction(
        category=category,
        action_type=action_type,
        decision=decision,
        confidence=confidence,
        player_id=player_id,
        record_id=record_id,
        table_name=table_name,
        field_name=field_name,
        current_value=current_value,
        proposed_value=proposed_value,
        related_player_ids="|".join(
            str(value) for value in sorted(set(related_player_ids or []))
        ),
        source_check=source_check,
        reason=reason,
        review_note=review_note,
    )


async def _duplicate_actions(conn: Any) -> list[CleanupAction]:
    """Create manual-review actions for duplicate prospect identities."""
    rows = await _fetch_all(
        conn,
        """
        SELECT
            pm.id AS player_id,
            pm.display_name,
            pm.school,
            pm.school_raw,
            pm.draft_year,
            pm.is_stub,
            pl.expected_draft_year,
            pl.lifecycle_stage,
            pl.draft_status,
            pl.is_draft_prospect,
            coalesce(stats.stats_count, 0) AS stats_count,
            coalesce(aliases.alias_count, 0) AS alias_count,
            coalesce(externals.external_count, 0) AS external_count,
            coalesce(images.image_count, 0) AS image_count
        FROM players_master pm
        LEFT JOIN player_lifecycle pl ON pl.player_id = pm.id
        LEFT JOIN (
            SELECT player_id, count(*) AS stats_count
            FROM player_college_stats
            GROUP BY player_id
        ) stats ON stats.player_id = pm.id
        LEFT JOIN (
            SELECT player_id, count(*) AS alias_count
            FROM player_aliases
            GROUP BY player_id
        ) aliases ON aliases.player_id = pm.id
        LEFT JOIN (
            SELECT player_id, count(*) AS external_count
            FROM player_external_ids
            GROUP BY player_id
        ) externals ON externals.player_id = pm.id
        LEFT JOIN (
            SELECT player_id, count(*) AS image_count
            FROM player_image_assets
            GROUP BY player_id
        ) images ON images.player_id = pm.id
        WHERE pm.display_name IS NOT NULL
          AND (
            pm.draft_year BETWEEN :start_year AND :end_year
            OR pl.expected_draft_year BETWEEN :start_year AND :end_year
            OR pl.is_draft_prospect IS true
            OR pm.is_stub IS true
          )
        ORDER BY pm.id
        """,
        {"start_year": PROSPECT_START_YEAR, "end_year": PROSPECT_END_YEAR},
    )
    by_name: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        normalized = normalize_player_name(str(row["display_name"] or ""))
        if normalized:
            by_name.setdefault(normalized, []).append(row)

    actions: list[CleanupAction] = []
    for normalized, duplicate_rows in sorted(by_name.items()):
        player_ids = [int(row["player_id"]) for row in duplicate_rows]
        if len(set(player_ids)) < 2:
            continue
        details = "; ".join(
            f"{row['player_id']}:{row['display_name']}:school={row['school'] or ''}:"
            f"draft_year={row['draft_year'] or ''}:stats={row['stats_count']}:"
            f"aliases={row['alias_count']}:external_ids={row['external_count']}:"
            f"images={row['image_count']}:stage={row['lifecycle_stage'] or ''}:"
            f"draft_status={row['draft_status'] or ''}"
            for row in duplicate_rows
        )
        actions.append(
            _action(
                category="duplicate_identity",
                action_type="review_merge_plan",
                decision="manual_review",
                confidence="medium",
                player_id=None,
                table_name="players_master",
                related_player_ids=player_ids,
                source_check="duplicate_normalized_display_name",
                reason=f"Normalized display name '{normalized}' maps to multiple prospect-scope players.",
                review_note=f"Create a reviewed keep/discard merge plan before any write. Candidates: {details}",
            )
        )
    return actions


async def _bad_image_actions(conn: Any) -> list[CleanupAction]:
    """Create cleanup actions for known-bad image URL fields."""
    player_rows = await _fetch_all(
        conn,
        """
        SELECT id AS player_id, display_name, reference_image_url
        FROM players_master
        WHERE reference_image_url IS NOT NULL
          AND reference_image_url <> ''
        ORDER BY id
        """,
    )
    actions: list[CleanupAction] = []
    for row in player_rows:
        reason = _bad_url_reason(str(row["reference_image_url"]))
        if not reason:
            continue
        actions.append(
            _action(
                category="bad_image",
                action_type="clear_bad_reference_image_url",
                decision="execute_candidate",
                confidence="high",
                player_id=int(row["player_id"]),
                table_name="players_master",
                field_name="reference_image_url",
                current_value=str(row["reference_image_url"]),
                proposed_value="",
                source_check="bad_player_reference_image_url",
                reason=reason,
                review_note="Known-bad reference image URL should be cleared or replaced before image generation.",
            )
        )

    asset_rows = await _fetch_all(
        conn,
        """
        SELECT id AS record_id, player_id, public_url, reference_image_url
        FROM player_image_assets
        WHERE reference_image_url IS NOT NULL
          AND reference_image_url <> ''
        ORDER BY id
        """,
    )
    for row in asset_rows:
        reason = _bad_url_reason(str(row["reference_image_url"]))
        if not reason:
            continue
        actions.append(
            _action(
                category="bad_image",
                action_type="review_or_regenerate_image_asset",
                decision="manual_review",
                confidence="medium",
                player_id=int(row["player_id"]),
                record_id=int(row["record_id"]),
                table_name="player_image_assets",
                field_name="reference_image_url",
                current_value=str(row["reference_image_url"]),
                proposed_value="review_regenerate_or_clear",
                source_check="bad_player_image_asset_url",
                reason=reason,
                review_note=(
                    "Generated asset retains a bad reference URL; review whether the public asset "
                    "should be kept, regenerated, or removed."
                ),
            )
        )
    return actions


async def _affiliation_actions(conn: Any) -> list[CleanupAction]:
    """Classify unmapped affiliations into waiver/backlog/manual review actions."""
    rows = await _fetch_all(
        conn,
        """
        SELECT
            pm.id AS player_id,
            pm.display_name,
            pm.school,
            pm.school_raw,
            pm.draft_year,
            pl.expected_draft_year,
            pl.lifecycle_stage,
            pl.current_affiliation_name,
            pl.current_affiliation_type,
            pl.is_draft_prospect
        FROM players_master pm
        LEFT JOIN player_lifecycle pl ON pl.player_id = pm.id
        WHERE pm.draft_year BETWEEN :start_year AND :end_year
           OR pl.expected_draft_year BETWEEN :start_year AND :end_year
           OR pl.is_draft_prospect IS true
           OR pm.is_stub IS true
        ORDER BY pm.id
        """,
        {"start_year": PROSPECT_START_YEAR, "end_year": PROSPECT_END_YEAR},
    )
    mapping = load_school_mapping()
    college_school_names = load_college_school_names()
    top100 = _top100_names()
    actions: list[CleanupAction] = []
    seen: set[tuple[int, str]] = set()
    for row in rows:
        player_id = int(row["player_id"])
        display_name = str(row["display_name"] or "")
        normalized = normalize_player_name(display_name)
        values = {
            "school_raw": _compact(row["school_raw"]).strip(),
            "school": _compact(row["school"]).strip(),
            "current_affiliation_name": _compact(
                row["current_affiliation_name"]
            ).strip(),
        }
        for field_name, raw_value in values.items():
            if not raw_value or (player_id, raw_value) in seen:
                continue
            seen.add((player_id, raw_value))
            resolution = resolve_affiliation(raw_value, mapping, college_school_names)
            if resolution.resolution_status != "needs_review":
                continue

            cleaned_value = _clean_affiliation(raw_value)
            if cleaned_value in NBA_TEAM_NAMES:
                decision = "waive_candidate"
                confidence = "high"
                action_type = "waive_nba_team_affiliation"
                note = "NBA team affiliation is valid non-college data; mapping audit should not block promotion on this row."
            elif normalized in top100:
                decision = "manual_review"
                confidence = "high"
                action_type = "review_top100_affiliation_mapping"
                note = "Top 100 player has unmapped affiliation in prod; resolve or waive before promotion."
            elif row["is_draft_prospect"] is True:
                decision = "manual_review"
                confidence = "medium"
                action_type = "review_current_prospect_affiliation"
                note = "Current prospect-scope player has unmapped affiliation; classify source before promotion waiver."
            else:
                decision = "backlog"
                confidence = "medium"
                action_type = "defer_historical_affiliation_cleanup"
                note = "Historical/non-current affiliation cleanup can be tracked outside Top 100 promotion."

            actions.append(
                _action(
                    category="affiliation",
                    action_type=action_type,
                    decision=decision,
                    confidence=confidence,
                    player_id=player_id,
                    table_name="players_master"
                    if field_name in {"school", "school_raw"}
                    else "player_lifecycle",
                    field_name=field_name,
                    current_value=raw_value,
                    proposed_value="waive_or_map",
                    source_check="unmapped_prospect_affiliation",
                    reason="unmapped_affiliation",
                    review_note=f"{display_name}: {note}",
                )
            )
    return actions


def _lifecycle_actions(plan_path: Path) -> list[CleanupAction]:
    """Convert lifecycle cleanup plan rows into combined cleanup actions."""
    actions: list[CleanupAction] = []
    top100 = _top100_names()
    with plan_path.open(newline="", encoding="utf-8") as file:
        for row in csv.DictReader(file):
            player_id = int(row["player_id"])
            is_top100 = row["is_top100_source"] == "True"
            normalized = row["normalized_name"]
            original_classification = row["classification"]

            if is_top100 or normalized in top100:
                decision = "manual_review"
                confidence = "high"
                action_type = "review_top100_lifecycle_alignment"
                note = (
                    "Frozen Top 100 source conflicts with or is missing prod lifecycle data; "
                    "review before applying promotion data."
                )
            elif original_classification == "fix_now":
                decision = "execute_candidate"
                confidence = row["confidence"] or "high"
                action_type = "set_is_draft_prospect_false"
                note = "High-confidence stale prospect flag based on non-prospect lifecycle/draft status."
            elif original_classification == "manual_review":
                decision = "manual_review"
                confidence = row["confidence"] or "low"
                action_type = "review_missing_expected_draft_year"
                note = "Prospect flag is true but expected draft year is missing."
            else:
                decision = "backlog"
                confidence = row["confidence"] or "medium"
                action_type = "defer_stale_historical_prospect_flag"
                note = "Likely historical lifecycle cleanup; defer unless it impacts current prospect filters."

            current_value = (
                f"is_draft_prospect={row['is_draft_prospect']};"
                f"expected_draft_year={row['expected_draft_year']};"
                f"draft_year={row['draft_year']};"
                f"draft_status={row['draft_status']};"
                f"lifecycle_stage={row['lifecycle_stage']}"
            )
            proposed_value = (
                f"is_draft_prospect={row['proposed_is_draft_prospect']};"
                f"expected_draft_year={row['proposed_expected_draft_year']}"
            )
            actions.append(
                _action(
                    category="lifecycle",
                    action_type=action_type,
                    decision=decision,
                    confidence=confidence,
                    player_id=player_id,
                    table_name="player_lifecycle",
                    field_name="is_draft_prospect|expected_draft_year",
                    current_value=current_value,
                    proposed_value=proposed_value,
                    source_check="draft_prospect_lifecycle_cleanup_plan",
                    reason=row["reason"],
                    review_note=f"{row['display_name']}: {note}",
                )
            )
    return actions


async def generate_prod_cleanup_plan(
    output_date: date,
    database_url: str,
) -> tuple[Path, Path, list[CleanupAction]]:
    """Generate the combined prod cleanup plan CSV and summary note."""
    lifecycle_plan_path = await generate_cleanup_plan(
        output_date=output_date,
        environment="prod",
        database_url=database_url,
    )
    cleaned_url, connect_args = _prepare_connection(database_url)
    engine = create_async_engine(cleaned_url, echo=False, connect_args=connect_args)
    try:
        async with engine.connect() as conn:
            actions = [
                *await _duplicate_actions(conn),
                *await _bad_image_actions(conn),
                *_lifecycle_actions(lifecycle_plan_path),
                *await _affiliation_actions(conn),
            ]
    finally:
        await engine.dispose()

    decision_order = {
        "execute_candidate": 0,
        "manual_review": 1,
        "waive_candidate": 2,
        "backlog": 3,
    }
    actions.sort(
        key=lambda action: (
            decision_order.get(action.decision, 9),
            action.category,
            action.player_id or 0,
            action.record_id or 0,
        )
    )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = OUTPUT_DIR / f"prod_top100_cleanup_plan_{output_date.isoformat()}.csv"
    summary_path = (
        OUTPUT_DIR / f"prod_top100_cleanup_plan_summary_{output_date.isoformat()}.md"
    )
    with csv_path.open("w", newline="", encoding="utf-8") as file:
        fieldnames = list(
            asdict(
                CleanupAction(
                    "",
                    "",
                    "",
                    "",
                    None,
                    None,
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                )
            )
        )
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(asdict(action) for action in actions)

    counts: dict[tuple[str, str], int] = {}
    for action in actions:
        key = (action.decision, action.category)
        counts[key] = counts.get(key, 0) + 1
    lines = [
        f"# Prod Top 100 Cleanup Plan - {output_date.isoformat()}",
        "",
        "## Artifacts",
        "",
        f"- Combined cleanup plan: `{csv_path}`",
        f"- Lifecycle cleanup plan: `{lifecycle_plan_path}`",
        "",
        "## Decision Counts",
        "",
    ]
    for (decision, category), count in sorted(counts.items()):
        lines.append(f"- {decision} / {category}: {count}")
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "- `execute_candidate` rows are likely safe to implement after review and dry-run.",
            "- `manual_review` rows need explicit review before any production write.",
            "- `waive_candidate` rows look valid but should be documented as waivers.",
            "- `backlog` rows are real cleanup items that should not block Top 100 promotion by default.",
            "",
            "No production data was modified by this plan.",
        ]
    )
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return csv_path, summary_path, actions


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description="Generate prod Top 100 cleanup plan")
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
        csv_path, summary_path, actions = asyncio.run(
            generate_prod_cleanup_plan(args.date, args.database_url)
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    print(csv_path)
    print(summary_path)
    print(f"cleanup_actions={len(actions)}")


if __name__ == "__main__":
    main()
