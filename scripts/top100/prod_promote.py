"""Promote the reviewed 2026 Top 100 source board into production.

The script creates missing Top 100 player stubs and aligns matched Top 100 rows
to the current draft lifecycle scope. It writes a dry-run/execute manifest and
does not merge or delete existing players.

Usage:
    conda run -n draftguru python scripts/top100/prod_promote.py --dry-run
    conda run -n draftguru python scripts/top100/prod_promote.py --execute
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

from app.services.canonical_resolution_service import (  # noqa: E402
    load_college_school_names,
    load_school_mapping,
    resolve_affiliation,
)
from app.services.player_mention_service import parse_player_name  # noqa: E402
from app.utils.slug import generate_slug_sync  # noqa: E402
from scripts.top100.audit import resolve_rows  # noqa: E402
from scripts.top100.refresh import OUTPUT_DIR, TOP100_ROWS, _prepare_connection  # noqa: E402


CURRENT_DRAFT_YEAR = 2026
SOURCE_KEY = "top100_source_2026"


@dataclass(frozen=True, slots=True)
class PromotionRow:
    """One Top 100 promotion decision."""

    source_rank: int
    source_name: str
    raw_affiliation: str
    canonical_affiliation: str
    affiliation_type: str
    source_position: str
    source_height: str
    match_status: str
    old_player_id: int | None
    new_player_id: int | None
    action: str
    slug: str
    lifecycle_stage: str
    competition_context: str
    current_affiliation_name: str
    current_affiliation_type: str
    note: str


def _height_to_inches(value: str) -> int | None:
    """Convert source height like 6-8.5 into whole inches."""
    if "-" not in value:
        return None
    feet_text, inches_text = value.split("-", 1)
    try:
        return round(int(feet_text) * 12 + float(inches_text))
    except ValueError:
        return None


def _lifecycle_values(
    *,
    raw_affiliation: str,
    canonical_affiliation: str,
    affiliation_type: str,
) -> tuple[str, str, str, str]:
    """Return lifecycle enum names and affiliation display value."""
    if affiliation_type == "college":
        return (
            "COLLEGE",
            "NCAA",
            canonical_affiliation,
            "COLLEGE_TEAM",
        )
    return (
        "PRO_NON_NBA",
        "OVERSEAS_PRO",
        raw_affiliation,
        "OVERSEAS_CLUB",
    )


def _manifest_fieldnames() -> list[str]:
    """Return manifest CSV fieldnames."""
    return list(
        asdict(
            PromotionRow(
                source_rank=0,
                source_name="",
                raw_affiliation="",
                canonical_affiliation="",
                affiliation_type="",
                source_position="",
                source_height="",
                match_status="",
                old_player_id=None,
                new_player_id=None,
                action="",
                slug="",
                lifecycle_stage="",
                competition_context="",
                current_affiliation_name="",
                current_affiliation_type="",
                note="",
            )
        )
    )


async def _existing_slugs(conn: Any) -> set[str]:
    """Fetch existing non-empty player slugs."""
    result = await conn.execute(
        text("SELECT slug FROM players_master WHERE slug IS NOT NULL AND slug <> ''")
    )
    return {str(row["slug"]) for row in result.mappings()}


async def _insert_player(
    conn: Any,
    *,
    source_name: str,
    raw_affiliation: str,
    canonical_affiliation: str,
    affiliation_type: str,
    slug: str,
    now: datetime,
) -> int:
    """Insert a missing Top 100 player stub and return its id."""
    parsed = parse_player_name(source_name)
    school = canonical_affiliation if affiliation_type == "college" else None
    result = await conn.execute(
        text(
            """
            INSERT INTO players_master
                (
                    slug, first_name, middle_name, last_name, suffix, display_name,
                    school, school_raw, draft_year, is_stub, bio_source,
                    created_at, updated_at
                )
            VALUES
                (
                    :slug, :first_name, :middle_name, :last_name, :suffix,
                    :display_name, :school, :school_raw, :draft_year, true,
                    :bio_source, :now, :now
                )
            RETURNING id
            """
        ),
        {
            "slug": slug,
            "first_name": parsed.first_name or None,
            "middle_name": parsed.middle_name,
            "last_name": parsed.last_name,
            "suffix": parsed.suffix,
            "display_name": source_name,
            "school": school,
            "school_raw": raw_affiliation,
            "draft_year": CURRENT_DRAFT_YEAR,
            "bio_source": SOURCE_KEY,
            "now": now,
        },
    )
    return int(result.scalar_one())


async def _update_player_source_fields(
    conn: Any,
    *,
    player_id: int,
    raw_affiliation: str,
    canonical_affiliation: str,
    affiliation_type: str,
    now: datetime,
) -> None:
    """Align stable source-level fields for an existing Top 100 player."""
    await conn.execute(
        text(
            """
            UPDATE players_master
            SET draft_year = :draft_year,
                school = CASE
                    WHEN :affiliation_type = 'college' THEN :canonical_affiliation
                    ELSE school
                END,
                school_raw = :raw_affiliation,
                updated_at = :now
            WHERE id = :player_id
            """
        ),
        {
            "player_id": player_id,
            "draft_year": CURRENT_DRAFT_YEAR,
            "raw_affiliation": raw_affiliation,
            "canonical_affiliation": canonical_affiliation,
            "affiliation_type": affiliation_type,
            "now": now,
        },
    )


async def _ensure_alias(
    conn: Any,
    *,
    player_id: int,
    source_name: str,
    context: str,
) -> None:
    """Ensure the source-board spelling exists as an alias."""
    parsed = parse_player_name(source_name)
    await conn.execute(
        text(
            """
            INSERT INTO player_aliases
                (player_id, full_name, first_name, middle_name, last_name, suffix, context, created_at)
            VALUES
                (:player_id, :full_name, :first_name, :middle_name, :last_name, :suffix, :context, now())
            ON CONFLICT ON CONSTRAINT uq_player_aliases_player_fullname DO NOTHING
            """
        ),
        {
            "player_id": player_id,
            "full_name": source_name,
            "first_name": parsed.first_name or None,
            "middle_name": parsed.middle_name,
            "last_name": parsed.last_name,
            "suffix": parsed.suffix,
            "context": context,
        },
    )


async def _upsert_status(
    conn: Any,
    *,
    player_id: int,
    raw_position: str,
    height_in: int | None,
    now: datetime,
) -> None:
    """Upsert source-board status fields."""
    await conn.execute(
        text(
            """
            INSERT INTO player_status
                (player_id, raw_position, height_in, source, updated_at)
            VALUES
                (:player_id, :raw_position, :height_in, :source, :now)
            ON CONFLICT ON CONSTRAINT uq_player_status_player DO UPDATE
            SET raw_position = COALESCE(player_status.raw_position, EXCLUDED.raw_position),
                height_in = COALESCE(player_status.height_in, EXCLUDED.height_in),
                source = CASE
                    WHEN player_status.source IS NULL THEN EXCLUDED.source
                    ELSE player_status.source
                END,
                updated_at = :now
            """
        ),
        {
            "player_id": player_id,
            "raw_position": raw_position,
            "height_in": height_in,
            "source": SOURCE_KEY,
            "now": now,
        },
    )


async def _upsert_lifecycle(
    conn: Any,
    *,
    player_id: int,
    lifecycle_stage: str,
    competition_context: str,
    current_affiliation_name: str,
    current_affiliation_type: str,
    now: datetime,
) -> None:
    """Upsert current Top 100 lifecycle scope."""
    await conn.execute(
        text(
            """
            INSERT INTO player_lifecycle
                (
                    player_id, lifecycle_stage, competition_context, draft_status,
                    expected_draft_year, current_affiliation_name,
                    current_affiliation_type, commitment_status, is_draft_prospect,
                    source, confidence, updated_at
                )
            VALUES
                (
                    :player_id, :lifecycle_stage, :competition_context, 'ELIGIBLE',
                    :expected_draft_year, :current_affiliation_name,
                    :current_affiliation_type, 'UNKNOWN', true,
                    :source, :confidence, :now
                )
            ON CONFLICT ON CONSTRAINT uq_player_lifecycle_player DO UPDATE
            SET lifecycle_stage = EXCLUDED.lifecycle_stage,
                competition_context = EXCLUDED.competition_context,
                draft_status = EXCLUDED.draft_status,
                expected_draft_year = EXCLUDED.expected_draft_year,
                current_affiliation_name = EXCLUDED.current_affiliation_name,
                current_affiliation_type = EXCLUDED.current_affiliation_type,
                is_draft_prospect = EXCLUDED.is_draft_prospect,
                source = EXCLUDED.source,
                confidence = EXCLUDED.confidence,
                updated_at = EXCLUDED.updated_at
            """
        ),
        {
            "player_id": player_id,
            "lifecycle_stage": lifecycle_stage,
            "competition_context": competition_context,
            "expected_draft_year": CURRENT_DRAFT_YEAR,
            "current_affiliation_name": current_affiliation_name,
            "current_affiliation_type": current_affiliation_type,
            "source": SOURCE_KEY,
            "confidence": 0.85,
            "now": now,
        },
    )


def _write_manifest(path: Path, rows: list[PromotionRow]) -> None:
    """Write promotion rows to CSV."""
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=_manifest_fieldnames())
        writer.writeheader()
        writer.writerows(asdict(row) for row in rows)


async def promote_top100(
    *,
    database_url: str,
    output_date: date,
    execute: bool,
) -> tuple[Path, Path, list[PromotionRow]]:
    """Promote Top 100 rows and write a manifest/log."""
    resolved_rows = await resolve_rows(database_url)
    source_by_rank = {row.source_rank: row for row in TOP100_ROWS}
    mapping = load_school_mapping()
    college_school_names = load_college_school_names()
    cleaned_url, connect_args = _prepare_connection(database_url)
    engine = create_async_engine(cleaned_url, echo=False, connect_args=connect_args)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    mode = "execute" if execute else "dry_run"
    manifest_path = (
        OUTPUT_DIR / f"prod_top100_promotion_{mode}_{output_date.isoformat()}.csv"
    )
    log_path = (
        OUTPUT_DIR / f"prod_top100_promotion_{mode}_{output_date.isoformat()}.log"
    )
    manifest_rows: list[PromotionRow] = []

    try:
        async with engine.begin() as conn:
            existing_slugs = await _existing_slugs(conn)
            now = datetime.now(UTC).replace(tzinfo=None)
            for resolved in resolved_rows:
                source_row = source_by_rank[resolved.source_rank]
                affiliation = resolve_affiliation(
                    source_row.source_affiliation,
                    mapping,
                    college_school_names,
                )
                (
                    lifecycle_stage,
                    competition_context,
                    current_affiliation_name,
                    current_affiliation_type,
                ) = _lifecycle_values(
                    raw_affiliation=source_row.source_affiliation,
                    canonical_affiliation=affiliation.canonical_affiliation,
                    affiliation_type=affiliation.affiliation_type,
                )
                player_id = resolved.player_id
                action = "update_matched"
                slug = ""
                note = "Matched player aligned to Top 100 source lifecycle."

                if player_id is None:
                    action = "create_stub"
                    slug = generate_slug_sync(source_row.source_name, existing_slugs)
                    existing_slugs.add(slug)
                    note = "Missing Top 100 source row will be created as a reviewable stub."
                    if execute:
                        player_id = await _insert_player(
                            conn,
                            source_name=source_row.source_name,
                            raw_affiliation=source_row.source_affiliation,
                            canonical_affiliation=affiliation.canonical_affiliation,
                            affiliation_type=affiliation.affiliation_type,
                            slug=slug,
                            now=now,
                        )

                if execute and player_id is not None:
                    await _update_player_source_fields(
                        conn,
                        player_id=player_id,
                        raw_affiliation=source_row.source_affiliation,
                        canonical_affiliation=affiliation.canonical_affiliation,
                        affiliation_type=affiliation.affiliation_type,
                        now=now,
                    )
                    await _ensure_alias(
                        conn,
                        player_id=player_id,
                        source_name=source_row.source_name,
                        context=SOURCE_KEY,
                    )
                    await _upsert_status(
                        conn,
                        player_id=player_id,
                        raw_position=source_row.source_position,
                        height_in=_height_to_inches(source_row.source_height),
                        now=now,
                    )
                    await _upsert_lifecycle(
                        conn,
                        player_id=player_id,
                        lifecycle_stage=lifecycle_stage,
                        competition_context=competition_context,
                        current_affiliation_name=current_affiliation_name,
                        current_affiliation_type=current_affiliation_type,
                        now=now,
                    )

                manifest_rows.append(
                    PromotionRow(
                        source_rank=resolved.source_rank,
                        source_name=source_row.source_name,
                        raw_affiliation=source_row.source_affiliation,
                        canonical_affiliation=affiliation.canonical_affiliation,
                        affiliation_type=affiliation.affiliation_type,
                        source_position=source_row.source_position,
                        source_height=source_row.source_height,
                        match_status=resolved.match_status,
                        old_player_id=resolved.player_id,
                        new_player_id=player_id,
                        action=action,
                        slug=slug,
                        lifecycle_stage=lifecycle_stage,
                        competition_context=competition_context,
                        current_affiliation_name=current_affiliation_name,
                        current_affiliation_type=current_affiliation_type,
                        note=note,
                    )
                )
    finally:
        await engine.dispose()

    _write_manifest(manifest_path, manifest_rows)
    action_counts: dict[str, int] = {}
    for row in manifest_rows:
        action_counts[row.action] = action_counts.get(row.action, 0) + 1
    log_lines = [
        f"mode={mode}",
        f"executed_at={datetime.now(UTC).isoformat()}",
        f"manifest={manifest_path}",
        f"promotion_rows={len(manifest_rows)}",
        *[f"{action}={count}" for action, count in sorted(action_counts.items())],
        "created_or_planned_player_ids="
        + "|".join(
            str(row.new_player_id)
            for row in manifest_rows
            if row.action == "create_stub" and row.new_player_id is not None
        ),
    ]
    log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    return manifest_path, log_path, manifest_rows


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Promote reviewed Top 100 rows to prod"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Write manifest only")
    mode.add_argument("--execute", action="store_true", help="Apply promotion updates")
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
        manifest_path, log_path, rows = asyncio.run(
            promote_top100(
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
    print(f"promotion_rows={len(rows)}")


if __name__ == "__main__":
    main()
