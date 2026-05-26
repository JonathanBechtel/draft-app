"""Backfill player embeddings for PlayerMaster rows that lack a vector.

Iterates over all ``players_master`` rows that have no entry in
``player_embeddings``, builds the embed input for each, calls Gemini in
configurable batches, and writes the resulting vectors to the table.

Usage::

    # Dry run (count only, no API calls):
    python scripts/backfill_player_embeddings.py --dry-run

    # Live run with default batch size (50):
    python scripts/backfill_player_embeddings.py

    # Custom batch size:
    python scripts/backfill_player_embeddings.py --batch-size 25

Requires ``DATABASE_URL`` and ``GEMINI_API_KEY`` (or
``GEMINI_SUMMARIZATION_API_KEY``) to be set in the environment or
``.env`` file.

.. note::
    This script makes live Gemini API calls for every player without an
    existing embedding.  On a ~5,900-row dataset that is roughly 120
    batches at the default size.  Run deliberately — the orchestrator
    triggers this during the QA pass, not during normal CI.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import Callable, Coroutine
from typing import Any

from dotenv import load_dotenv

# Ensure repo root is on sys.path so ``app`` is importable when the script is
# executed directly (i.e., ``python scripts/backfill_player_embeddings.py``).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

load_dotenv()

from sqlalchemy import select  # noqa: E402
from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.schemas.player_embeddings import PlayerEmbedding  # noqa: E402
from app.schemas.players_master import PlayerMaster  # noqa: E402
from app.services.embedding_service import embed_players_batch  # noqa: E402
from app.config import settings  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

_DEFAULT_BATCH_SIZE = 50


async def fetch_players_missing_embeddings(db: AsyncSession) -> list[PlayerMaster]:
    """Return all PlayerMaster rows that have no entry in player_embeddings.

    Uses a LEFT JOIN / IS NULL pattern so the query runs in a single round
    trip rather than a Python-side set difference.

    Args:
        db: Active async session.

    Returns:
        List of ``PlayerMaster`` instances ordered by id.
    """
    stmt = (
        select(PlayerMaster)  # type: ignore[call-overload]
        .outerjoin(
            PlayerEmbedding,
            PlayerMaster.id == PlayerEmbedding.player_id,  # type: ignore[arg-type]
        )
        .where(PlayerEmbedding.player_id.is_(None))  # type: ignore[union-attr,attr-defined]
        .order_by(PlayerMaster.id)  # type: ignore[call-overload]
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


EmbedFn = Callable[[list[PlayerMaster]], Coroutine[Any, Any, list[list[float]]]]


async def backfill(
    *,
    batch_size: int = _DEFAULT_BATCH_SIZE,
    dry_run: bool = False,
    embed_fn: EmbedFn | None = None,
) -> int:
    """Run the backfill and return the number of rows written.

    Args:
        batch_size: Number of players to embed per Gemini API call.
        dry_run: When ``True``, count rows but make no API calls or writes.
        embed_fn: Optional override for the embed function (injected in unit
            tests to avoid live Gemini calls).  Must have the same signature
            as ``embed_players_batch``.

    Returns:
        Total number of embedding rows written (0 in dry-run mode).
    """
    _embed = embed_fn if embed_fn is not None else embed_players_batch

    database_url = os.getenv("DATABASE_URL") or settings.database_url
    if not database_url:
        logger.error("DATABASE_URL is not set — cannot connect to Postgres.")
        sys.exit(1)

    engine = create_async_engine(database_url, echo=False)
    session_factory = async_sessionmaker(
        bind=engine, expire_on_commit=False, class_=AsyncSession
    )

    total_written = 0

    async with session_factory() as db:
        players = await fetch_players_missing_embeddings(db)
        logger.info("Found %d player(s) without embeddings.", len(players))

        if dry_run:
            logger.info("Dry run — skipping API calls and writes.")
            await engine.dispose()
            return 0

        # Process in batches.
        for batch_start in range(0, len(players), batch_size):
            batch = players[batch_start : batch_start + batch_size]
            batch_end = batch_start + len(batch)
            logger.info(
                "Embedding players %d–%d of %d …",
                batch_start + 1,
                batch_end,
                len(players),
            )

            try:
                vectors = await _embed(batch)
            except Exception:
                logger.exception(
                    "Embedding API call failed for batch %d–%d; skipping.",
                    batch_start + 1,
                    batch_end,
                )
                continue

            rows = [
                {
                    "player_id": player.id,
                    "embedding": vector,
                    "model_name": settings.gemini_embedding_model,
                }
                for player, vector in zip(batch, vectors)
                if player.id is not None
            ]

            if not rows:
                continue

            async with db.begin():
                stmt = (
                    pg_insert(PlayerEmbedding)
                    .values(rows)
                    .on_conflict_do_nothing(index_elements=["player_id"])
                )
                await db.execute(stmt)

            total_written += len(rows)
            logger.info(
                "Wrote %d embedding row(s) (running total: %d).",
                len(rows),
                total_written,
            )

    await engine.dispose()
    logger.info("Backfill complete — %d row(s) written.", total_written)
    return total_written


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill player_embeddings for players that lack a vector."
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=_DEFAULT_BATCH_SIZE,
        help=f"Number of players per Gemini API call (default: {_DEFAULT_BATCH_SIZE}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count rows but skip API calls and DB writes.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    asyncio.run(backfill(batch_size=args.batch_size, dry_run=args.dry_run))
