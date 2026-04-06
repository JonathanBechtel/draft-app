"""One-time deduplication of duplicate player records in the 2026 draft class.

Merges duplicate pairs by:
1. Reassigning all child records from discard → keep (handling unique conflicts)
2. Adding the discarded name as an alias on the kept record
3. Deleting the discarded player record

Usage:
    # Dry run (default) — shows what would happen
    conda run -n draftguru python scripts/deduplicate_players.py

    # Execute for real
    conda run -n draftguru python scripts/deduplicate_players.py --execute
"""

import argparse
import asyncio
import os
import ssl
import sys
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


def _prepare_connection(url: str) -> tuple[str, dict]:
    """Strip Neon query params that asyncpg doesn't accept."""
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
        elif key == "channel_binding":
            pass  # asyncpg doesn't support this
        else:
            filtered.append((key, value))

    cleaned = urlunsplit(split._replace(query=urlencode(filtered))).rstrip("?")

    connect_args: dict = {}
    if sslmode and sslmode.lower() == "require":
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        connect_args["ssl"] = ctx

    return cleaned, connect_args


# (keep_id, discard_id, canonical_display_name)
MERGE_PAIRS: list[tuple[int, int, str]] = [
    (5409, 6113, "Cameron Boozer"),  # Cam Boozer → Cameron Boozer
    (5403, 6179, "Motiejus Krivas"),  # Mo Krivas → Motiejus Krivas
    (5423, 5405, "Patrick Ngongba II"),  # Pat Ngongba II → Patrick Ngongba II
    (5431, 6186, "Zvonimir Ivisic"),  # Zvonimir Ivišić → Zvonimir Ivisic
    (6338, 6345, "Joseph Tugler"),  # Jojo Tugler → Joseph Tugler
    (6192, 6191, "William Kyle III"),  # William Kyle → William Kyle III
    (6202, 6238, "Terrance Arceneaux"),  # Terrence Arceneaux → Terrance Arceneaux
    (1709, 6310, "Labaron Philon"),  # Stub Jr. → existing record; fix draft_year
]

# Players whose draft_year should be corrected after merge.
DRAFT_YEAR_FIXES: dict[int, int] = {
    1709: 2026,  # Labaron Philon — sophomore, entering 2026 draft
}

# Tables with player_id FK and their unique constraints that could conflict.
# Format: (table, player_id_column, conflict_columns_besides_player_id | None)
CHILD_TABLES: list[tuple[str, str, list[str] | None]] = [
    ("player_content_mentions", "player_id", ["content_type", "content_id"]),
    ("player_college_stats", "player_id", ["season"]),
    ("player_aliases", "player_id", None),
    ("player_bio_snapshots", "player_id", None),
    ("player_external_ids", "player_id", None),
    ("player_status", "player_id", None),
    ("player_metric_values", "player_id", None),
    ("player_similarity", "anchor_player_id", None),
    ("player_similarity", "comparison_player_id", None),
    ("player_image_assets", "player_id", None),
    ("pending_image_previews", "player_id", None),
    ("combine_anthro", "player_id", None),
    ("combine_agility", "player_id", None),
    ("combine_shooting_results", "player_id", None),
    ("news_items", "player_id", None),
    ("podcast_episodes", "player_id", None),
]


async def merge_pair(
    conn: Any,
    keep_id: int,
    discard_id: int,
    canonical_name: str,
    dry_run: bool,
) -> None:
    """Merge discard_id into keep_id."""
    print(
        f"\n{'[DRY RUN] ' if dry_run else ''}Merging {discard_id} → {keep_id} ({canonical_name})"
    )

    # Fetch discard's display name for the alias
    row = (
        await conn.execute(  # type: ignore[attr-defined]
            text("SELECT display_name FROM players_master WHERE id = :id"),
            {"id": discard_id},
        )
    ).fetchone()
    if not row:
        print(f"  ⚠ Discard ID {discard_id} not found, skipping")
        return
    discard_name: str = row[0]

    # Reassign child records
    for table, col, conflict_cols in CHILD_TABLES:
        # Count how many rows would be affected
        count_row = (
            await conn.execute(  # type: ignore[attr-defined]
                text(f"SELECT count(*) FROM {table} WHERE {col} = :discard_id"),
                {"discard_id": discard_id},
            )
        ).fetchone()
        count = count_row[0] if count_row else 0
        if count == 0:
            continue

        if conflict_cols:
            # Delete rows that would violate unique constraints, then update the rest
            conflict_where = " AND ".join(f"d.{c} = k.{c}" for c in conflict_cols)
            delete_sql = f"""
                DELETE FROM {table} d
                USING {table} k
                WHERE d.{col} = :discard_id
                  AND k.{col} = :keep_id
                  AND {conflict_where}
            """
            update_sql = f"""
                UPDATE {table} SET {col} = :keep_id WHERE {col} = :discard_id
            """
            if dry_run:
                # Count conflicts
                conflict_sql = f"""
                    SELECT count(*) FROM {table} d
                    JOIN {table} k ON k.{col} = :keep_id AND {conflict_where}
                    WHERE d.{col} = :discard_id
                """
                conflict_row = (
                    await conn.execute(  # type: ignore[attr-defined]
                        text(conflict_sql),
                        {"discard_id": discard_id, "keep_id": keep_id},
                    )
                ).fetchone()
                conflicts = conflict_row[0] if conflict_row else 0
                print(
                    f"  {table}.{col}: {count} rows ({conflicts} conflicts to delete, {count - conflicts} to reassign)"
                )
            else:
                await conn.execute(  # type: ignore[attr-defined]
                    text(delete_sql), {"discard_id": discard_id, "keep_id": keep_id}
                )
                await conn.execute(  # type: ignore[attr-defined]
                    text(update_sql), {"discard_id": discard_id, "keep_id": keep_id}
                )
                print(f"  {table}.{col}: merged {count} rows")
        else:
            if dry_run:
                print(f"  {table}.{col}: {count} rows to reassign")
            else:
                await conn.execute(  # type: ignore[attr-defined]
                    text(
                        f"UPDATE {table} SET {col} = :keep_id WHERE {col} = :discard_id"
                    ),
                    {"discard_id": discard_id, "keep_id": keep_id},
                )
                print(f"  {table}.{col}: reassigned {count} rows")

    # Add discard's name as alias (if different from keep's name)
    if discard_name.lower() != canonical_name.lower():
        if dry_run:
            print(f"  Will add alias: '{discard_name}' → player {keep_id}")
        else:
            await conn.execute(  # type: ignore[attr-defined]
                text("""
                    INSERT INTO player_aliases (player_id, full_name, context, created_at)
                    VALUES (:player_id, :full_name, 'dedup_merge', now())
                    ON CONFLICT DO NOTHING
                """),
                {"player_id": keep_id, "full_name": discard_name},
            )
            print(f"  Added alias: '{discard_name}'")

    # Fix draft_year if needed
    if keep_id in DRAFT_YEAR_FIXES:
        new_year = DRAFT_YEAR_FIXES[keep_id]
        if dry_run:
            print(f"  Will update draft_year → {new_year} for player {keep_id}")
        else:
            await conn.execute(  # type: ignore[attr-defined]
                text("UPDATE players_master SET draft_year = :year WHERE id = :id"),
                {"year": new_year, "id": keep_id},
            )
            print(f"  Updated draft_year → {new_year}")

    # Delete the discarded player
    if dry_run:
        print(f"  Will delete player {discard_id} ({discard_name})")
    else:
        await conn.execute(  # type: ignore[attr-defined]
            text("DELETE FROM players_master WHERE id = :id"),
            {"id": discard_id},
        )
        print(f"  Deleted player {discard_id} ({discard_name})")


async def main(dry_run: bool) -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL not set")
        sys.exit(1)

    cleaned_url, connect_args = _prepare_connection(database_url)
    engine = create_async_engine(cleaned_url, echo=False, connect_args=connect_args)

    mode = "DRY RUN" if dry_run else "EXECUTING"
    print(f"=== Player Deduplication ({mode}) ===")
    print(f"Merging {len(MERGE_PAIRS)} duplicate pairs")

    async with engine.begin() as conn:
        for keep_id, discard_id, canonical_name in MERGE_PAIRS:
            await merge_pair(conn, keep_id, discard_id, canonical_name, dry_run)

        if dry_run:
            print("\n--- Dry run complete. Run with --execute to apply. ---")
            await conn.rollback()
        else:
            print("\n--- All merges committed. ---")

    await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Deduplicate player records")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually execute the merges (default is dry run)",
    )
    args = parser.parse_args()
    asyncio.run(main(dry_run=not args.execute))
