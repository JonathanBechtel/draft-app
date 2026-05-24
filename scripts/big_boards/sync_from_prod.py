"""Sync APPROVED big boards from a prod snapshot into the dev database.

Usage:
    SNAP_URL=postgresql://... DEV_URL=postgresql://... python sync_big_boards_from_prod.py

Maps FKs by stable keys (news_source.name, players_master.slug) so prod
IDs never leak into dev. Copies any source row missing in dev. Skips any
board that already exists in dev (matched by source name + draft_year +
published_at), so re-runs are idempotent.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Optional

import asyncpg


def _normalize(url: str) -> str:
    """Strip the asyncpg+sqlalchemy scheme and any query params."""
    url = url.strip().replace("postgresql+asyncpg://", "postgresql://")
    return url.split("?")[0]


async def _ensure_source(
    dev: asyncpg.Connection,
    snap_row: asyncpg.Record,
) -> int:
    """Return the dev news_sources.id for the given prod row, creating if missing."""
    existing = await dev.fetchval(
        "SELECT id FROM news_sources WHERE name = $1", snap_row["name"]
    )
    if existing is not None:
        return existing
    new_id = await dev.fetchval(
        """
        INSERT INTO news_sources (
            name, display_name, feed_type, feed_url, is_active,
            is_draft_focused, fetch_interval_minutes, created_at, updated_at
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        RETURNING id
        """,
        snap_row["name"],
        snap_row["display_name"],
        snap_row["feed_type"],
        snap_row["feed_url"],
        snap_row["is_active"],
        snap_row["is_draft_focused"],
        snap_row["fetch_interval_minutes"],
        snap_row["created_at"],
        snap_row["updated_at"],
    )
    print(f"  + created dev news_source: {snap_row['name']} (id={new_id})")
    return new_id


async def _sync_one_board(
    snap: asyncpg.Connection,
    dev: asyncpg.Connection,
    prod_board: asyncpg.Record,
    source_name: str,
    dev_source_id: int,
    player_id_map: dict[int, int],
) -> Optional[int]:
    """Insert a prod board + its entries into dev. Returns dev board_id or None if skipped."""
    existing = await dev.fetchval(
        """
        SELECT id FROM big_boards
        WHERE news_source_id = $1
          AND draft_year = $2
          AND published_at = $3
        """,
        dev_source_id,
        prod_board["draft_year"],
        prod_board["published_at"],
    )
    if existing is not None:
        print(
            f"  = skip {source_name} {prod_board['draft_year']} "
            f"@{prod_board['published_at']} (already in dev as id={existing})"
        )
        return None

    dev_board_id = await dev.fetchval(
        """
        INSERT INTO big_boards (
            news_source_id, news_item_id, draft_year, published_at,
            board_size, status, approved_at, created_at, updated_at
        )
        VALUES ($1, NULL, $2, $3, $4, $5, $6, $7, $8)
        RETURNING id
        """,
        dev_source_id,
        prod_board["draft_year"],
        prod_board["published_at"],
        prod_board["board_size"],
        prod_board["status"],
        prod_board["approved_at"],
        prod_board["created_at"],
        prod_board["updated_at"],
    )

    prod_entries = await snap.fetch(
        """
        SELECT player_id, rank, tier
        FROM big_board_entries
        WHERE board_id = $1
        ORDER BY rank
        """,
        prod_board["id"],
    )
    inserted = 0
    for e in prod_entries:
        dev_player_id = player_id_map.get(e["player_id"])
        if dev_player_id is None:
            print(
                f"    ! WARN: player_id={e['player_id']} on rank={e['rank']} "
                "has no dev mapping; skipping entry."
            )
            continue
        await dev.execute(
            """
            INSERT INTO big_board_entries (board_id, player_id, rank, tier)
            VALUES ($1, $2, $3, $4)
            """,
            dev_board_id,
            dev_player_id,
            e["rank"],
            e["tier"],
        )
        inserted += 1
    print(
        f"  + copied {source_name} {prod_board['draft_year']} "
        f"@{prod_board['published_at'].date()} -> dev board id={dev_board_id}, "
        f"{inserted}/{len(prod_entries)} entries"
    )
    return dev_board_id


async def main() -> int:
    snap_url = os.environ.get("SNAP_URL")
    dev_url = os.environ.get("DEV_URL")
    if not snap_url or not dev_url:
        print("SNAP_URL and DEV_URL env vars required", file=sys.stderr)
        return 2

    snap = await asyncpg.connect(_normalize(snap_url))
    dev = await asyncpg.connect(_normalize(dev_url))

    try:
        prod_boards = await snap.fetch(
            """
            SELECT bb.*, ns.name AS source_name
            FROM big_boards bb
            JOIN news_sources ns ON ns.id = bb.news_source_id
            WHERE bb.status = 'APPROVED'
            ORDER BY bb.id
            """
        )
        print(f"Found {len(prod_boards)} APPROVED prod boards")

        # Cache dev source lookups so we don't hit the DB per board.
        source_id_cache: dict[str, int] = {}

        # Build player_id map (prod -> dev) by slug.
        prod_slugs = [
            r["slug"]
            for r in await snap.fetch(
                "SELECT DISTINCT pm.slug FROM big_board_entries bbe "
                "JOIN players_master pm ON pm.id = bbe.player_id"
            )
        ]
        prod_player_lookup = {
            r["slug"]: r["id"]
            for r in await snap.fetch(
                "SELECT id, slug FROM players_master WHERE slug = ANY($1::text[])",
                prod_slugs,
            )
        }
        dev_player_lookup = {
            r["slug"]: r["id"]
            for r in await dev.fetch(
                "SELECT id, slug FROM players_master WHERE slug = ANY($1::text[])",
                prod_slugs,
            )
        }
        player_id_map = {
            prod_id: dev_player_lookup[slug]
            for slug, prod_id in prod_player_lookup.items()
            if slug in dev_player_lookup
        }
        print(f"Player FK map size: {len(player_id_map)} / {len(prod_player_lookup)}")

        copied = skipped = 0
        for prod_board in prod_boards:
            src_name = prod_board["source_name"]
            if src_name not in source_id_cache:
                snap_src = await snap.fetchrow(
                    "SELECT * FROM news_sources WHERE id = $1",
                    prod_board["news_source_id"],
                )
                assert snap_src is not None
                source_id_cache[src_name] = await _ensure_source(dev, snap_src)
            dev_source_id = source_id_cache[src_name]

            result = await _sync_one_board(
                snap, dev, prod_board, src_name, dev_source_id, player_id_map
            )
            if result is None:
                skipped += 1
            else:
                copied += 1

        print(f"\nDone. copied={copied} skipped={skipped}")
        return 0
    finally:
        await snap.close()
        await dev.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
