"""Generate read-only prospect integrity audit artifacts.

This script is intended for the Top 100 refresh QA gate. It can be run against
dev, stage, or prod and writes reviewable CSV/Markdown artifacts without
mutating data.

Usage:
    conda run -n draftguru python scripts/top100/prospect_integrity_audit.py \
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
from urllib.parse import urlparse

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
from scripts.top100.refresh import OUTPUT_DIR, _prepare_connection  # noqa: E402


PROSPECT_START_YEAR = 2025
PROSPECT_END_YEAR = 2028


@dataclass(frozen=True, slots=True)
class Finding:
    """One reviewable integrity finding."""

    check_name: str
    classification: str
    severity: str
    record_count: int
    player_ids: str
    record_ids: str
    summary: str
    details: str
    recommended_action: str


def _csv_value(value: object) -> str:
    """Return a compact string for CSV detail fields."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _bad_url_reason(url: str | None) -> str:
    """Return why a URL is structurally known-bad, if applicable."""
    if not url:
        return ""
    lower_url = url.lower().strip()
    if lower_url.endswith(".pdf") or ".pdf?" in lower_url or ".pdf#" in lower_url:
        return "pdf_url"
    if "archive.org" in lower_url:
        return "archive_or_document_url"
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return "malformed_url"
    return ""


def _finding(
    check_name: str,
    *,
    classification: str,
    severity: str,
    rows: list[dict[str, Any]],
    player_ids: list[int] | None = None,
    record_ids: list[int] | None = None,
    summary: str,
    details: str,
    recommended_action: str,
) -> Finding:
    """Build a finding row with stable CSV formatting."""
    return Finding(
        check_name=check_name,
        classification=classification,
        severity=severity,
        record_count=len(rows),
        player_ids="|".join(
            str(player_id) for player_id in sorted(set(player_ids or []))
        ),
        record_ids="|".join(
            str(record_id) for record_id in sorted(set(record_ids or []))
        ),
        summary=summary,
        details=details,
        recommended_action=recommended_action,
    )


async def _fetch_all(
    conn: Any, sql: str, params: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Execute a query and return mapping rows."""
    result = await conn.execute(text(sql), params or {})
    return [dict(row) for row in result.mappings()]


async def _fetch_counts(conn: Any) -> dict[str, int]:
    """Fetch high-level table counts for the summary note."""
    rows = await _fetch_all(
        conn,
        """
        SELECT 'players_master' AS table_name, count(*) AS row_count FROM players_master
        UNION ALL SELECT 'prospect_scope', count(*) FROM (
            SELECT pm.id
            FROM players_master pm
            LEFT JOIN player_lifecycle pl ON pl.player_id = pm.id
            WHERE pm.draft_year BETWEEN :start_year AND :end_year
               OR pl.expected_draft_year BETWEEN :start_year AND :end_year
               OR pl.is_draft_prospect IS true
               OR pm.is_stub IS true
        ) scoped
        UNION ALL SELECT 'player_aliases', count(*) FROM player_aliases
        UNION ALL SELECT 'player_external_ids', count(*) FROM player_external_ids
        UNION ALL SELECT 'player_college_stats', count(*) FROM player_college_stats
        UNION ALL SELECT 'player_image_assets', count(*) FROM player_image_assets
        UNION ALL SELECT 'player_content_mentions', count(*) FROM player_content_mentions
        """,
        {"start_year": PROSPECT_START_YEAR, "end_year": PROSPECT_END_YEAR},
    )
    return {str(row["table_name"]): int(row["row_count"]) for row in rows}


async def _duplicate_identity_findings(conn: Any) -> list[Finding]:
    """Find duplicate normalized player names in prospect-scoped identities."""
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
            pl.is_draft_prospect
        FROM players_master pm
        LEFT JOIN player_lifecycle pl ON pl.player_id = pm.id
        WHERE pm.display_name IS NOT NULL
          AND (
            pm.draft_year BETWEEN :start_year AND :end_year
            OR pl.expected_draft_year BETWEEN :start_year AND :end_year
            OR pl.is_draft_prospect IS true
            OR pm.is_stub IS true
          )
        """,
        {"start_year": PROSPECT_START_YEAR, "end_year": PROSPECT_END_YEAR},
    )
    by_key: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        key = normalize_player_name(str(row["display_name"] or ""))
        if key:
            by_key.setdefault(key, []).append(row)

    findings: list[Finding] = []
    for key, duplicate_rows in sorted(by_key.items()):
        player_ids = [int(row["player_id"]) for row in duplicate_rows]
        if len(set(player_ids)) < 2:
            continue
        detail_parts = [
            f"{row['player_id']}:{row['display_name']}:{row['school'] or ''}:{row['draft_year'] or ''}"
            for row in duplicate_rows
        ]
        findings.append(
            _finding(
                "duplicate_normalized_display_name",
                classification="blocking",
                severity="high",
                rows=duplicate_rows,
                player_ids=player_ids,
                summary=f"Normalized prospect display name '{key}' maps to multiple players.",
                details="; ".join(detail_parts),
                recommended_action="Review records and merge or waive as distinct people.",
            )
        )
    return findings


async def _alias_findings(conn: Any) -> list[Finding]:
    """Find aliases that resolve to multiple players after normalization."""
    rows = await _fetch_all(
        conn,
        """
        SELECT
            pa.id AS alias_id,
            pa.player_id,
            pa.full_name,
            pm.display_name,
            pm.draft_year,
            pl.expected_draft_year,
            pl.is_draft_prospect
        FROM player_aliases pa
        JOIN players_master pm ON pm.id = pa.player_id
        LEFT JOIN player_lifecycle pl ON pl.player_id = pm.id
        WHERE pa.full_name IS NOT NULL
          AND (
            pm.draft_year BETWEEN :start_year AND :end_year
            OR pl.expected_draft_year BETWEEN :start_year AND :end_year
            OR pl.is_draft_prospect IS true
            OR pm.is_stub IS true
          )
        """,
        {"start_year": PROSPECT_START_YEAR, "end_year": PROSPECT_END_YEAR},
    )
    by_key: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        key = normalize_player_name(str(row["full_name"] or ""))
        if key:
            by_key.setdefault(key, []).append(row)

    findings: list[Finding] = []
    for key, alias_rows in sorted(by_key.items()):
        player_ids = [int(row["player_id"]) for row in alias_rows]
        if len(set(player_ids)) < 2:
            continue
        details = "; ".join(
            f"{row['alias_id']}:{row['full_name']}->{row['player_id']}:{row['display_name']}"
            for row in alias_rows
        )
        findings.append(
            _finding(
                "alias_maps_to_multiple_players",
                classification="blocking",
                severity="high",
                rows=alias_rows,
                player_ids=player_ids,
                record_ids=[int(row["alias_id"]) for row in alias_rows],
                summary=f"Normalized alias '{key}' maps to multiple prospect players.",
                details=details,
                recommended_action="Review alias ownership; merge duplicates or remove incorrect aliases.",
            )
        )
    return findings


async def _external_id_findings(conn: Any) -> list[Finding]:
    """Find external IDs attached to multiple players."""
    rows = await _fetch_all(
        conn,
        """
        SELECT
            system,
            external_id,
            count(*) AS record_count,
            count(DISTINCT player_id) AS player_count,
            string_agg(id::text, '|' ORDER BY id) AS external_row_ids,
            string_agg(player_id::text, '|' ORDER BY player_id) AS player_ids
        FROM player_external_ids
        GROUP BY system, external_id
        HAVING count(DISTINCT player_id) > 1
        ORDER BY system, external_id
        """,
    )
    findings: list[Finding] = []
    for row in rows:
        player_ids = [
            int(value) for value in str(row["player_ids"]).split("|") if value
        ]
        record_ids = [
            int(value) for value in str(row["external_row_ids"]).split("|") if value
        ]
        findings.append(
            _finding(
                "conflicting_external_id",
                classification="blocking",
                severity="high",
                rows=[row],
                player_ids=player_ids,
                record_ids=record_ids,
                summary=f"{row['system']}:{row['external_id']} is attached to multiple players.",
                details=f"player_ids={row['player_ids']}; external_row_ids={row['external_row_ids']}",
                recommended_action="Review source ownership and keep the external ID on one canonical player.",
            )
        )
    return findings


async def _orphan_findings(conn: Any) -> list[Finding]:
    """Find rows with player references that do not resolve to players_master."""
    table_specs = (
        ("player_aliases", "id", "player_id"),
        ("player_external_ids", "id", "player_id"),
        ("player_college_stats", "id", "player_id"),
        ("player_status", "id", "player_id"),
        ("player_lifecycle", "id", "player_id"),
        ("player_bio_snapshots", "id", "player_id"),
        ("player_image_assets", "id", "player_id"),
        ("pending_image_previews", "id", "player_id"),
        ("news_items", "id", "player_id"),
        ("podcast_episodes", "id", "player_id"),
        ("player_metric_values", "id", "player_id"),
        ("combine_anthro", "id", "player_id"),
        ("combine_agility", "id", "player_id"),
        ("combine_shooting_results", "id", "player_id"),
        ("player_content_mentions", "id", "player_id"),
    )
    findings: list[Finding] = []
    for table_name, id_column, player_column in table_specs:
        rows = await _fetch_all(
            conn,
            f"""
            SELECT child.{id_column} AS record_id, child.{player_column} AS player_id
            FROM {table_name} child
            LEFT JOIN players_master pm ON pm.id = child.{player_column}
            WHERE child.{player_column} IS NOT NULL
              AND pm.id IS NULL
            ORDER BY child.{id_column}
            LIMIT 200
            """,
        )
        if not rows:
            continue
        findings.append(
            _finding(
                f"orphaned_{table_name}",
                classification="blocking",
                severity="high",
                rows=rows,
                player_ids=[int(row["player_id"]) for row in rows],
                record_ids=[int(row["record_id"]) for row in rows],
                summary=f"{table_name} contains player_id references missing from players_master.",
                details="; ".join(
                    f"{row['record_id']}->player:{row['player_id']}"
                    for row in rows[:25]
                ),
                recommended_action="Reassign to canonical players or delete orphaned child rows.",
            )
        )
    return findings


async def _polymorphic_mention_findings(conn: Any) -> list[Finding]:
    """Find player_content_mentions whose content target is missing."""
    specs = (
        ("NEWS", "news_items"),
        ("PODCAST", "podcast_episodes"),
        ("VIDEO", "youtube_videos"),
    )
    findings: list[Finding] = []
    for content_type, table_name in specs:
        rows = await _fetch_all(
            conn,
            f"""
            SELECT pcm.id AS record_id, pcm.player_id, pcm.content_id
            FROM player_content_mentions pcm
            LEFT JOIN {table_name} content ON content.id = pcm.content_id
            WHERE pcm.content_type = :content_type
              AND content.id IS NULL
            ORDER BY pcm.id
            LIMIT 200
            """,
            {"content_type": content_type},
        )
        if not rows:
            continue
        findings.append(
            _finding(
                f"orphaned_{content_type.lower()}_content_mention",
                classification="separate_backlog",
                severity="medium",
                rows=rows,
                player_ids=[int(row["player_id"]) for row in rows],
                record_ids=[int(row["record_id"]) for row in rows],
                summary=f"{content_type} player mentions point at missing content rows.",
                details="; ".join(
                    f"{row['record_id']}->content:{row['content_id']}"
                    for row in rows[:25]
                ),
                recommended_action="Clean stale content mentions outside Top 100 promotion if not blocking player identity.",
            )
        )
    return findings


async def _image_url_findings(conn: Any) -> list[Finding]:
    """Find structurally bad image URLs in player records and generated assets."""
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
    bad_player_rows = [
        {**row, "bad_reason": _bad_url_reason(str(row["reference_image_url"]))}
        for row in player_rows
        if _bad_url_reason(str(row["reference_image_url"]))
    ]

    asset_rows = await _fetch_all(
        conn,
        """
        SELECT id AS record_id, player_id, public_url, reference_image_url
        FROM player_image_assets
        WHERE public_url IS NOT NULL
           OR reference_image_url IS NOT NULL
        ORDER BY id
        """,
    )
    bad_asset_rows: list[dict[str, Any]] = []
    for row in asset_rows:
        public_reason = (
            _bad_url_reason(str(row["public_url"])) if row["public_url"] else ""
        )
        reference_reason = (
            _bad_url_reason(str(row["reference_image_url"]))
            if row["reference_image_url"]
            else ""
        )
        if public_reason:
            bad_asset_rows.append(
                {
                    **row,
                    "bad_field": "public_url",
                    "bad_url": row["public_url"],
                    "bad_reason": public_reason,
                }
            )
        if reference_reason:
            bad_asset_rows.append(
                {
                    **row,
                    "bad_field": "reference_image_url",
                    "bad_url": row["reference_image_url"],
                    "bad_reason": reference_reason,
                }
            )

    findings: list[Finding] = []
    if bad_player_rows:
        findings.append(
            _finding(
                "bad_player_reference_image_url",
                classification="blocking",
                severity="high",
                rows=bad_player_rows,
                player_ids=[int(row["player_id"]) for row in bad_player_rows],
                summary="Player reference image URLs include known-bad URL shapes.",
                details="; ".join(
                    f"{row['player_id']}:{row['bad_reason']}:{row['reference_image_url']}"
                    for row in bad_player_rows[:25]
                ),
                recommended_action="Replace or clear PDF/archive/malformed reference image URLs.",
            )
        )
    if bad_asset_rows:
        findings.append(
            _finding(
                "bad_player_image_asset_url",
                classification="blocking",
                severity="high",
                rows=bad_asset_rows,
                player_ids=[int(row["player_id"]) for row in bad_asset_rows],
                record_ids=[int(row["record_id"]) for row in bad_asset_rows],
                summary="Player image asset URLs include known-bad URL shapes.",
                details="; ".join(
                    f"{row['record_id']}:{row['bad_field']}:{row['bad_reason']}:{row['bad_url']}"
                    for row in bad_asset_rows[:25]
                ),
                recommended_action="Replace generated/public asset URLs before relying on image rendering.",
            )
        )
    return findings


async def _unmapped_affiliation_findings(conn: Any) -> list[Finding]:
    """Find prospect-scoped school/affiliation values not covered by mapping."""
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
            pl.current_affiliation_name
        FROM players_master pm
        LEFT JOIN player_lifecycle pl ON pl.player_id = pm.id
        WHERE (
            pm.draft_year BETWEEN :start_year AND :end_year
            OR pl.expected_draft_year BETWEEN :start_year AND :end_year
            OR pl.is_draft_prospect IS true
            OR pm.is_stub IS true
        )
        ORDER BY pm.id
        """,
        {"start_year": PROSPECT_START_YEAR, "end_year": PROSPECT_END_YEAR},
    )
    mapping = load_school_mapping()
    college_school_names = load_college_school_names()
    bad_rows: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()
    for row in rows:
        values = [
            _csv_value(row["school_raw"]).strip(),
            _csv_value(row["school"]).strip(),
            _csv_value(row["current_affiliation_name"]).strip(),
        ]
        for raw_value in values:
            if not raw_value:
                continue
            key = (int(row["player_id"]), raw_value)
            if key in seen:
                continue
            seen.add(key)
            resolution = resolve_affiliation(raw_value, mapping, college_school_names)
            if resolution.resolution_status == "needs_review":
                bad_rows.append(
                    {
                        **row,
                        "raw_affiliation": raw_value,
                        "resolution_status": resolution.resolution_status,
                    }
                )

    if not bad_rows:
        return []
    return [
        _finding(
            "unmapped_prospect_affiliation",
            classification="blocking",
            severity="high",
            rows=bad_rows,
            player_ids=[int(row["player_id"]) for row in bad_rows],
            summary="Prospect-scoped affiliation values are not covered by reviewed mapping.",
            details="; ".join(
                f"{row['player_id']}:{row['display_name']}:{row['raw_affiliation']}"
                for row in bad_rows[:50]
            ),
            recommended_action="Add reviewed mappings or explicit non-college waivers before promotion.",
        )
    ]


async def _draft_status_findings(conn: Any) -> list[Finding]:
    """Find stale or contradictory prospect status/draft-year fields."""
    rows = await _fetch_all(
        conn,
        """
        SELECT
            pm.id AS player_id,
            pm.display_name,
            pm.draft_year,
            pm.is_stub,
            pl.lifecycle_stage,
            pl.competition_context,
            pl.draft_status,
            pl.expected_draft_year,
            pl.is_draft_prospect,
            pl.current_affiliation_name
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
    missing_lifecycle = [row for row in rows if row["lifecycle_stage"] is None]
    missing_expected_year = [
        row
        for row in rows
        if row["is_draft_prospect"] is True and row["expected_draft_year"] is None
    ]
    drafted_still_prospect = [
        row
        for row in rows
        if row["draft_status"] == "DRAFTED" and row["is_draft_prospect"] is True
    ]
    old_draft_year_prospect = [
        row
        for row in rows
        if row["draft_year"] is not None
        and int(row["draft_year"]) < PROSPECT_START_YEAR
        and row["is_draft_prospect"] is True
    ]

    findings: list[Finding] = []
    if missing_lifecycle:
        findings.append(
            _finding(
                "missing_prospect_lifecycle",
                classification="blocking",
                severity="high",
                rows=missing_lifecycle,
                player_ids=[int(row["player_id"]) for row in missing_lifecycle],
                summary="Prospect-scoped players are missing lifecycle rows.",
                details="; ".join(
                    f"{row['player_id']}:{row['display_name']}"
                    for row in missing_lifecycle[:50]
                ),
                recommended_action="Create or backfill player_lifecycle rows.",
            )
        )
    if missing_expected_year:
        findings.append(
            _finding(
                "draft_prospect_missing_expected_year",
                classification="blocking",
                severity="medium",
                rows=missing_expected_year,
                player_ids=[int(row["player_id"]) for row in missing_expected_year],
                summary="Draft prospects are missing expected_draft_year.",
                details="; ".join(
                    f"{row['player_id']}:{row['display_name']}"
                    for row in missing_expected_year[:50]
                ),
                recommended_action="Backfill expected draft year or waive with source rationale.",
            )
        )
    if drafted_still_prospect:
        findings.append(
            _finding(
                "drafted_player_still_marked_prospect",
                classification="blocking",
                severity="medium",
                rows=drafted_still_prospect,
                player_ids=[int(row["player_id"]) for row in drafted_still_prospect],
                summary="Drafted players are still marked as draft prospects.",
                details="; ".join(
                    f"{row['player_id']}:{row['display_name']}:{row['draft_year']}"
                    for row in drafted_still_prospect[:50]
                ),
                recommended_action="Correct lifecycle/is_draft_prospect fields or waive if intentionally tracked.",
            )
        )
    if old_draft_year_prospect:
        findings.append(
            _finding(
                "old_draft_year_still_marked_prospect",
                classification="separate_backlog",
                severity="medium",
                rows=old_draft_year_prospect,
                player_ids=[int(row["player_id"]) for row in old_draft_year_prospect],
                summary="Players with old draft_year values remain marked as prospects.",
                details="; ".join(
                    f"{row['player_id']}:{row['display_name']}:{row['draft_year']}"
                    for row in old_draft_year_prospect[:50]
                ),
                recommended_action="Review historical prospect lifecycle cleanup outside Top 100 promotion.",
            )
        )
    return findings


async def generate_integrity_audit(
    output_date: date,
    environment: str,
    database_url: str,
) -> tuple[Path, Path, list[Finding], dict[str, int]]:
    """Generate CSV and Markdown integrity artifacts."""
    cleaned_url, connect_args = _prepare_connection(database_url)
    engine = create_async_engine(cleaned_url, echo=False, connect_args=connect_args)
    try:
        async with engine.connect() as conn:
            counts = await _fetch_counts(conn)
            findings = [
                *await _duplicate_identity_findings(conn),
                *await _alias_findings(conn),
                *await _external_id_findings(conn),
                *await _orphan_findings(conn),
                *await _polymorphic_mention_findings(conn),
                *await _image_url_findings(conn),
                *await _unmapped_affiliation_findings(conn),
                *await _draft_status_findings(conn),
            ]
    finally:
        await engine.dispose()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = (
        OUTPUT_DIR
        / f"{environment}_prospect_integrity_audit_{output_date.isoformat()}.csv"
    )
    summary_path = (
        OUTPUT_DIR
        / f"{environment}_prospect_integrity_summary_{output_date.isoformat()}.md"
    )

    with csv_path.open("w", newline="", encoding="utf-8") as file:
        fieldnames = list(asdict(Finding("", "", "", 0, "", "", "", "", "")))
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(asdict(finding) for finding in findings)

    blocking_count = sum(
        1 for finding in findings if finding.classification == "blocking"
    )
    backlog_count = sum(
        1 for finding in findings if finding.classification == "separate_backlog"
    )
    waiver_count = sum(
        1 for finding in findings if finding.classification == "waived_with_reason"
    )
    lines = [
        f"# {environment.title()} Prospect Integrity Audit - {output_date.isoformat()}",
        "",
        "## Scope Counts",
        "",
        *[
            f"- {table_name}: {row_count}"
            for table_name, row_count in sorted(counts.items())
        ],
        "",
        "## Finding Counts",
        "",
        f"- Total finding groups: {len(findings)}",
        f"- Blocking: {blocking_count}",
        f"- Waived with reason: {waiver_count}",
        f"- Separate backlog: {backlog_count}",
        "",
        "## Findings",
        "",
    ]
    if findings:
        for finding in findings:
            lines.extend(
                [
                    f"- {finding.classification} / {finding.severity} / {finding.check_name}: {finding.summary}",
                    f"  - Records: {finding.record_count}",
                    f"  - Player IDs: {finding.player_ids or 'n/a'}",
                    f"  - Recommended action: {finding.recommended_action}",
                ]
            )
    else:
        lines.append("- No findings.")
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return csv_path, summary_path, findings, counts


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description="Generate prospect integrity audit")
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
        csv_path, summary_path, findings, _counts = asyncio.run(
            generate_integrity_audit(args.date, args.environment, args.database_url)
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    print(csv_path)
    print(summary_path)
    print(f"finding_groups={len(findings)}")


if __name__ == "__main__":
    main()
