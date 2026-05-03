"""Clear known-bad production image reference URLs.

This script targets structurally invalid image reference URLs discovered during
Session #5 QA, primarily Wikimedia/Archive PDF URLs that were stored as player
likeness references. It does not delete generated image asset rows. Asset rows
that retain bad reference provenance are marked for human quality review by the
generated manifest.

Usage:
    conda run -n draftguru python scripts/top100/prod_bad_image_url_cleanup.py --dry-run
    conda run -n draftguru python scripts/top100/prod_bad_image_url_cleanup.py --execute
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

from scripts.top100.prospect_integrity_audit import _bad_url_reason  # noqa: E402
from scripts.top100.refresh import OUTPUT_DIR, _prepare_connection  # noqa: E402


@dataclass(frozen=True, slots=True)
class BadImageReviewRow:
    """One URL cleanup or image quality review row."""

    row_type: str
    player_id: int
    display_name: str
    asset_id: int | None
    field_name: str
    bad_reason: str
    bad_url: str
    public_url: str
    action: str
    review_note: str


async def _fetch_bad_rows(conn: Any) -> list[BadImageReviewRow]:
    """Fetch player and asset rows with known-bad reference URLs."""
    player_rows = (
        await conn.execute(
            text(
                """
                SELECT id AS player_id, display_name, reference_image_url
                FROM players_master
                WHERE reference_image_url IS NOT NULL
                  AND reference_image_url <> ''
                ORDER BY id
                """
            )
        )
    ).mappings()
    review_rows: list[BadImageReviewRow] = []
    for row in player_rows:
        bad_url = str(row["reference_image_url"])
        reason = _bad_url_reason(bad_url)
        if not reason:
            continue
        review_rows.append(
            BadImageReviewRow(
                row_type="player_reference",
                player_id=int(row["player_id"]),
                display_name=str(row["display_name"] or ""),
                asset_id=None,
                field_name="players_master.reference_image_url",
                bad_reason=reason,
                bad_url=bad_url,
                public_url="",
                action="set_null",
                review_note="Cleared known-bad player reference image URL.",
            )
        )

    asset_rows = (
        await conn.execute(
            text(
                """
                SELECT
                    pia.id AS asset_id,
                    pia.player_id,
                    pm.display_name,
                    pia.reference_image_url,
                    pia.public_url
                FROM player_image_assets pia
                JOIN players_master pm ON pm.id = pia.player_id
                WHERE pia.reference_image_url IS NOT NULL
                  AND pia.reference_image_url <> ''
                ORDER BY pia.id
                """
            )
        )
    ).mappings()
    for row in asset_rows:
        bad_url = str(row["reference_image_url"])
        reason = _bad_url_reason(bad_url)
        if not reason:
            continue
        review_rows.append(
            BadImageReviewRow(
                row_type="asset_reference",
                player_id=int(row["player_id"]),
                display_name=str(row["display_name"] or ""),
                asset_id=int(row["asset_id"]),
                field_name="player_image_assets.reference_image_url",
                bad_reason=reason,
                bad_url=bad_url,
                public_url=str(row["public_url"] or ""),
                action="set_null_keep_asset_for_quality_review",
                review_note=(
                    "Cleared bad asset reference URL; inspect generated asset quality "
                    "because the original likeness reference was invalid."
                ),
            )
        )
    return review_rows


def _write_manifest(path: Path, rows: list[BadImageReviewRow]) -> None:
    """Write review rows to CSV."""
    with path.open("w", newline="", encoding="utf-8") as file:
        fieldnames = list(
            asdict(BadImageReviewRow("", 0, "", None, "", "", "", "", "", ""))
        )
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(asdict(row) for row in rows)


async def cleanup_bad_urls(
    *,
    database_url: str,
    output_date: date,
    execute: bool,
) -> tuple[Path, Path, list[BadImageReviewRow]]:
    """Generate manifest and optionally clear bad image reference URLs."""
    cleaned_url, connect_args = _prepare_connection(database_url)
    engine = create_async_engine(cleaned_url, echo=False, connect_args=connect_args)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    mode = "execute" if execute else "dry_run"
    manifest_path = (
        OUTPUT_DIR / f"prod_bad_image_url_cleanup_{mode}_{output_date.isoformat()}.csv"
    )
    log_path = (
        OUTPUT_DIR / f"prod_bad_image_url_cleanup_{mode}_{output_date.isoformat()}.log"
    )

    try:
        async with engine.begin() as conn:
            review_rows = await _fetch_bad_rows(conn)
            _write_manifest(manifest_path, review_rows)

            player_ids = sorted(
                {
                    row.player_id
                    for row in review_rows
                    if row.row_type == "player_reference"
                }
            )
            asset_ids = sorted(
                {
                    row.asset_id
                    for row in review_rows
                    if row.row_type == "asset_reference" and row.asset_id is not None
                }
            )
            if execute:
                if player_ids:
                    await conn.execute(
                        text(
                            """
                            UPDATE players_master
                            SET reference_image_url = NULL,
                                updated_at = :updated_at
                            WHERE id = ANY(:player_ids)
                            """
                        ),
                        {
                            "player_ids": player_ids,
                            "updated_at": datetime.now(UTC).replace(tzinfo=None),
                        },
                    )
                if asset_ids:
                    await conn.execute(
                        text(
                            """
                            UPDATE player_image_assets
                            SET reference_image_url = NULL
                            WHERE id = ANY(:asset_ids)
                            """
                        ),
                        {"asset_ids": asset_ids},
                    )

    finally:
        await engine.dispose()

    lines = [
        f"mode={mode}",
        f"executed_at={datetime.now(UTC).isoformat()}",
        f"manifest={manifest_path}",
        f"player_reference_rows={sum(row.row_type == 'player_reference' for row in review_rows)}",
        f"asset_reference_rows={sum(row.row_type == 'asset_reference' for row in review_rows)}",
        "player_ids="
        + "|".join(
            str(row.player_id)
            for row in review_rows
            if row.row_type == "player_reference"
        ),
        "asset_ids="
        + "|".join(
            str(row.asset_id)
            for row in review_rows
            if row.row_type == "asset_reference" and row.asset_id is not None
        ),
    ]
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return manifest_path, log_path, review_rows


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description="Clear bad prod image reference URLs")
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
        manifest_path, log_path, rows = asyncio.run(
            cleanup_bad_urls(
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
    print(f"review_rows={len(rows)}")


if __name__ == "__main__":
    main()
