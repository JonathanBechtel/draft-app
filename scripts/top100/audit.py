"""Generate current dev audit artifacts for the Top 100 refresh.

This script is read-only. It resolves the frozen Top 100 source rows against
the current database and writes review CSVs for Session 3 enrichment work.

Usage:
    conda run -n draftguru python scripts/top100/audit.py --date 2026-04-27
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

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
from scripts.top100.refresh import (  # noqa: E402
    OUTPUT_DIR,
    TOP100_ROWS,
    _prepare_connection,
    fetch_player_candidates,
)


CURRENT_STATS_SEASON = "2025-26"
AUTHORITATIVE_STATS_SOURCES = {
    "sports_reference",
    "sports_reference_cbb",
    "manual_verified",
    "official",
}


@dataclass(frozen=True, slots=True)
class ResolvedRow:
    """A Top 100 source row with current DB resolution metadata."""

    source_rank: int
    source_name: str
    normalized_source_name: str
    raw_affiliation: str
    canonical_affiliation: str
    affiliation_type: str
    match_status: str
    candidate_count: int
    player_id: int | None
    candidate_player_ids: str
    reason: str


def _bad_image_reason(url: str | None) -> str:
    """Return why a reference image URL is known-bad, if applicable."""
    if not url:
        return ""
    lower_url = url.lower()
    if ".pdf" in lower_url or lower_url.endswith(".pdf"):
        return "pdf_url"
    if "ia_" in lower_url or "archive.org" in lower_url:
        return "archive_or_document_url"
    return ""


async def resolve_rows(database_url: str) -> list[ResolvedRow]:
    """Resolve all frozen Top 100 rows to current DB player IDs."""
    mapping = load_school_mapping()
    college_school_names = load_college_school_names()
    candidates_by_name, db_note = await fetch_player_candidates(database_url)
    resolved: list[ResolvedRow] = []

    for row in TOP100_ROWS:
        affiliation = resolve_affiliation(
            row.source_affiliation,
            mapping,
            college_school_names,
        )
        normalized_name = normalize_player_name(row.source_name)
        candidates = candidates_by_name.get(normalized_name, [])
        unique_candidates = {
            candidate["player_id"]: candidate
            for candidate in candidates
            if isinstance(candidate["player_id"], int)
        }
        candidate_ids = sorted(unique_candidates)

        if db_note:
            match_status = "needs_manual_review"
            player_id = None
            reason = db_note
        elif len(unique_candidates) == 1:
            match_status = "matched"
            player_id = candidate_ids[0]
            reason = "Normalized name resolved to one current player"
        elif len(unique_candidates) > 1:
            match_status = "merge_required"
            player_id = None
            reason = "Multiple current normalized-name candidates remain"
        else:
            match_status = "missing"
            player_id = None
            reason = "No current player matched this source row"

        resolved.append(
            ResolvedRow(
                source_rank=row.source_rank,
                source_name=row.source_name,
                normalized_source_name=normalized_name,
                raw_affiliation=row.source_affiliation,
                canonical_affiliation=affiliation.canonical_affiliation,
                affiliation_type=affiliation.affiliation_type,
                match_status=match_status,
                candidate_count=len(unique_candidates),
                player_id=player_id,
                candidate_player_ids="|".join(str(pid) for pid in candidate_ids),
                reason=reason,
            )
        )

    return resolved


async def fetch_detail_rows(
    conn: Any, player_ids: list[int]
) -> dict[int, dict[str, Any]]:
    """Fetch player detail, coverage, and provenance fields for audit output."""
    if not player_ids:
        return {}

    result = await conn.execute(
        text(
            """
            WITH latest_stats AS (
                SELECT DISTINCT ON (player_id)
                    player_id,
                    season AS latest_stats_season,
                    source AS latest_stats_source,
                    games,
                    ppg,
                    rpg,
                    apg
                FROM player_college_stats
                WHERE player_id = ANY(:player_ids)
                ORDER BY player_id, season DESC
            ),
            stats_summary AS (
                SELECT
                    player_id,
                    count(*) AS stats_count,
                    count(*) FILTER (
                        WHERE source = ANY(:authoritative_sources)
                    ) AS authoritative_stats_count,
                    count(*) FILTER (WHERE source = 'ai_generated') AS ai_stats_count,
                    string_agg(DISTINCT coalesce(source, ''), '|' ORDER BY coalesce(source, '')) AS stats_sources
                FROM player_college_stats
                WHERE player_id = ANY(:player_ids)
                GROUP BY player_id
            ),
            external_summary AS (
                SELECT
                    player_id,
                    string_agg(system || ':' || external_id, '|' ORDER BY system, external_id) AS external_ids,
                    bool_or(system = 'bbr') AS has_bbr_id
                FROM player_external_ids
                WHERE player_id = ANY(:player_ids)
                GROUP BY player_id
            ),
            alias_summary AS (
                SELECT player_id, count(*) AS alias_count
                FROM player_aliases
                WHERE player_id = ANY(:player_ids)
                GROUP BY player_id
            ),
            bio_summary AS (
                SELECT
                    player_id,
                    count(*) AS bio_snapshot_count,
                    string_agg(DISTINCT source, '|' ORDER BY source) AS bio_snapshot_sources
                FROM player_bio_snapshots
                WHERE player_id = ANY(:player_ids)
                GROUP BY player_id
            ),
            image_summary AS (
                SELECT player_id, count(*) AS image_asset_count
                FROM player_image_assets
                WHERE player_id = ANY(:player_ids)
                GROUP BY player_id
            )
            SELECT
                pm.id AS player_id,
                pm.slug,
                pm.display_name,
                pm.first_name,
                pm.middle_name,
                pm.last_name,
                pm.suffix,
                pm.birthdate,
                pm.birth_city,
                pm.birth_state_province,
                pm.birth_country,
                pm.school,
                pm.school_raw,
                pm.high_school,
                pm.shoots,
                pm.draft_year,
                pm.is_stub,
                pm.bio_source,
                pm.enrichment_attempted_at,
                pm.reference_image_url,
                pm.reference_image_s3_key,
                ps.raw_position,
                ps.height_in,
                ps.weight_lb,
                ps.source AS status_source,
                pl.lifecycle_stage,
                pl.competition_context,
                pl.draft_status,
                pl.expected_draft_year,
                pl.current_affiliation_name,
                pl.current_affiliation_type,
                pl.commitment_school,
                pl.commitment_status,
                pl.is_draft_prospect,
                pl.source AS lifecycle_source,
                coalesce(ss.stats_count, 0) AS stats_count,
                coalesce(ss.authoritative_stats_count, 0) AS authoritative_stats_count,
                coalesce(ss.ai_stats_count, 0) AS ai_stats_count,
                coalesce(ss.stats_sources, '') AS stats_sources,
                ls.latest_stats_season,
                ls.latest_stats_source,
                ls.games AS latest_games,
                ls.ppg AS latest_ppg,
                ls.rpg AS latest_rpg,
                ls.apg AS latest_apg,
                coalesce(es.external_ids, '') AS external_ids,
                coalesce(es.has_bbr_id, false) AS has_bbr_id,
                coalesce(alias_summary.alias_count, 0) AS alias_count,
                coalesce(bio_summary.bio_snapshot_count, 0) AS bio_snapshot_count,
                coalesce(bio_summary.bio_snapshot_sources, '') AS bio_snapshot_sources,
                coalesce(image_summary.image_asset_count, 0) AS image_asset_count
            FROM players_master pm
            LEFT JOIN player_status ps ON ps.player_id = pm.id
            LEFT JOIN player_lifecycle pl ON pl.player_id = pm.id
            LEFT JOIN stats_summary ss ON ss.player_id = pm.id
            LEFT JOIN latest_stats ls ON ls.player_id = pm.id
            LEFT JOIN external_summary es ON es.player_id = pm.id
            LEFT JOIN alias_summary ON alias_summary.player_id = pm.id
            LEFT JOIN bio_summary ON bio_summary.player_id = pm.id
            LEFT JOIN image_summary ON image_summary.player_id = pm.id
            WHERE pm.id = ANY(:player_ids)
            """
        ),
        {
            "player_ids": player_ids,
            "authoritative_sources": sorted(AUTHORITATIVE_STATS_SOURCES),
        },
    )
    return {int(row["player_id"]): dict(row) for row in result.mappings()}


def _stats_missing_reason(detail: dict[str, Any] | None, resolved: ResolvedRow) -> str:
    """Return a review reason for missing or non-authoritative stats."""
    if detail is None:
        return "no_player_match"
    if int(detail["stats_count"] or 0) == 0:
        if resolved.affiliation_type == "professional_or_international":
            return "needs_pro_or_international_source"
        if not detail["has_bbr_id"]:
            return "missing_bbr_external_id"
        return "needs_sports_reference_scrape"
    if int(detail["authoritative_stats_count"] or 0) == 0:
        return "authoritative_stats_needed"
    if detail["latest_stats_season"] != CURRENT_STATS_SEASON:
        return "current_season_stats_needed"
    return ""


def _enrichment_missing_reason(detail: dict[str, Any] | None) -> str:
    """Return a review reason for incomplete identity/bio/status enrichment."""
    if detail is None:
        return "no_player_match"
    missing = []
    if detail["is_stub"]:
        missing.append("stub_flag_retained")
    for field in ("first_name", "last_name", "school", "draft_year"):
        if detail[field] in (None, ""):
            missing.append(f"missing_{field}")
    if not detail["raw_position"]:
        missing.append("missing_status_position")
    if not detail["lifecycle_stage"]:
        missing.append("missing_lifecycle")
    if int(detail["bio_snapshot_count"] or 0) == 0 and not detail["bio_source"]:
        missing.append("missing_bio_provenance")
    return "|".join(missing)


def _base_output_row(
    resolved: ResolvedRow, detail: dict[str, Any] | None
) -> dict[str, Any]:
    """Build the shared audit fields for one source row."""
    image_bad_reason = _bad_image_reason(
        str(detail["reference_image_url"])
        if detail and detail["reference_image_url"]
        else None
    )
    has_reference_image = bool(
        detail
        and (
            detail["reference_image_url"]
            or detail["reference_image_s3_key"]
            or int(detail["image_asset_count"] or 0) > 0
        )
    )

    return {
        "source_rank": resolved.source_rank,
        "source_name": resolved.source_name,
        "normalized_source_name": resolved.normalized_source_name,
        "raw_affiliation": resolved.raw_affiliation,
        "canonical_affiliation": resolved.canonical_affiliation,
        "affiliation_type": resolved.affiliation_type,
        "match_status": resolved.match_status,
        "candidate_count": resolved.candidate_count,
        "candidate_player_ids": resolved.candidate_player_ids,
        "player_id": resolved.player_id or "",
        "match_reason": resolved.reason,
        "slug": detail["slug"] if detail else "",
        "db_display_name": detail["display_name"] if detail else "",
        "db_school": detail["school"] if detail else "",
        "db_school_raw": detail["school_raw"] if detail else "",
        "db_draft_year": detail["draft_year"] if detail else "",
        "is_stub": detail["is_stub"] if detail else "",
        "bio_source": detail["bio_source"] if detail else "",
        "bio_snapshot_count": detail["bio_snapshot_count"] if detail else 0,
        "bio_snapshot_sources": detail["bio_snapshot_sources"] if detail else "",
        "external_ids": detail["external_ids"] if detail else "",
        "has_bbr_id": detail["has_bbr_id"] if detail else False,
        "alias_count": detail["alias_count"] if detail else 0,
        "raw_position": detail["raw_position"] if detail else "",
        "height_in": detail["height_in"] if detail else "",
        "weight_lb": detail["weight_lb"] if detail else "",
        "status_source": detail["status_source"] if detail else "",
        "lifecycle_stage": detail["lifecycle_stage"] if detail else "",
        "competition_context": detail["competition_context"] if detail else "",
        "draft_status": detail["draft_status"] if detail else "",
        "expected_draft_year": detail["expected_draft_year"] if detail else "",
        "current_affiliation_name": detail["current_affiliation_name"]
        if detail
        else "",
        "current_affiliation_type": detail["current_affiliation_type"]
        if detail
        else "",
        "commitment_school": detail["commitment_school"] if detail else "",
        "commitment_status": detail["commitment_status"] if detail else "",
        "is_draft_prospect": detail["is_draft_prospect"] if detail else "",
        "lifecycle_source": detail["lifecycle_source"] if detail else "",
        "stats_count": detail["stats_count"] if detail else 0,
        "authoritative_stats_count": detail["authoritative_stats_count"]
        if detail
        else 0,
        "ai_stats_count": detail["ai_stats_count"] if detail else 0,
        "stats_sources": detail["stats_sources"] if detail else "",
        "latest_stats_season": detail["latest_stats_season"] if detail else "",
        "latest_stats_source": detail["latest_stats_source"] if detail else "",
        "latest_games": detail["latest_games"] if detail else "",
        "latest_ppg": detail["latest_ppg"] if detail else "",
        "latest_rpg": detail["latest_rpg"] if detail else "",
        "latest_apg": detail["latest_apg"] if detail else "",
        "reference_image_url": detail["reference_image_url"] if detail else "",
        "reference_image_s3_key": detail["reference_image_s3_key"] if detail else "",
        "image_asset_count": detail["image_asset_count"] if detail else 0,
        "image_missing": "" if has_reference_image else "yes",
        "image_bad_reason": image_bad_reason,
        "enrichment_missing_reason": _enrichment_missing_reason(detail),
        "stats_missing_reason": _stats_missing_reason(detail, resolved),
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    """Write rows to a CSV file."""
    if not rows:
        raise ValueError(f"No rows to write for {path}")
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


async def generate_audit(output_date: date, database_url: str) -> list[Path]:
    """Generate the Top 100 audit and stats review artifacts."""
    resolved_rows = await resolve_rows(database_url)
    player_ids = [row.player_id for row in resolved_rows if row.player_id is not None]

    cleaned_url, connect_args = _prepare_connection(database_url)
    engine = create_async_engine(cleaned_url, echo=False, connect_args=connect_args)
    try:
        async with engine.connect() as conn:
            details = await fetch_detail_rows(conn, player_ids)
    finally:
        await engine.dispose()

    audit_rows = [
        _base_output_row(row, details.get(row.player_id or -1)) for row in resolved_rows
    ]
    stats_rows = [
        {
            key: value
            for key, value in audit_row.items()
            if key
            in {
                "source_rank",
                "source_name",
                "raw_affiliation",
                "affiliation_type",
                "match_status",
                "player_id",
                "db_display_name",
                "db_school",
                "external_ids",
                "has_bbr_id",
                "stats_count",
                "authoritative_stats_count",
                "ai_stats_count",
                "stats_sources",
                "latest_stats_season",
                "latest_stats_source",
                "latest_games",
                "latest_ppg",
                "latest_rpg",
                "latest_apg",
                "stats_missing_reason",
            }
        }
        for audit_row in audit_rows
    ]

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    audit_path = OUTPUT_DIR / f"top100_dev_audit_{output_date.isoformat()}.csv"
    stats_path = OUTPUT_DIR / f"top100_stats_review_{output_date.isoformat()}.csv"
    write_csv(audit_path, audit_rows)
    write_csv(stats_path, stats_rows)
    return [audit_path, stats_path]


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description="Generate Top 100 dev audit CSVs")
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
        paths = asyncio.run(generate_audit(args.date, args.database_url))
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    for path in paths:
        print(path)


if __name__ == "__main__":
    main()
