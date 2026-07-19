"""Durable per-game completion tracking for chunked normalization batches.

``app.cli.summer_league_ingest_runner`` splits shot and PBP normalization for
one venue into small, independently committed game-id batches (see that
module's docstring and
``docs/plans/summer-league-cron-desk-starvation-spec.md``). This module is
what makes that resumable: each successfully committed batch durably records
which game IDs it covered, so a crash/interruption partway through a venue's
shot/PBP normalization -- or a batch deferred because the Desk writer took
priority -- resumes only the incomplete games on a later scheduled run
instead of replaying already-committed work.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.summer_league_pipeline import (
    SummerLeagueBatchPhase,
    SummerLeagueBatchProgress,
)

_TABLE = getattr(SummerLeagueBatchProgress, "__table__")


async def get_completed_batch_game_ids(
    db: AsyncSession,
    *,
    year: int,
    league_id: str,
    phase: SummerLeagueBatchPhase,
) -> set[str]:
    """Return the game IDs already durably completed for one phase/venue/year.

    Args:
        db: Active database session.
        year: Summer League season year.
        league_id: NBA Stats LeagueID for the venue.
        phase: Which batched normalization phase to check.

    Returns:
        The set of ``nba_stats_game_id`` values already recorded complete;
        empty when this phase/venue/year has never committed a batch.
    """
    result = await db.execute(
        select(_TABLE.c.game_id).where(
            _TABLE.c.year == year,
            _TABLE.c.league_id == league_id,
            _TABLE.c.phase == phase,
        )
    )
    return {row[0] for row in result.all()}


async def record_batch_progress(
    db: AsyncSession,
    *,
    year: int,
    league_id: str,
    phase: SummerLeagueBatchPhase,
    game_ids: Iterable[str],
    now: datetime | None = None,
) -> None:
    """Durably mark ``game_ids`` complete for one phase, idempotently.

    Call this inside the same ``db.begin()`` block as the batch's own
    normalization write so the progress marker only becomes durable exactly
    when the batch it describes actually commits -- a crash before commit
    leaves neither the normalized rows nor the progress marker behind, so a
    later run reprocesses that batch cleanly rather than skipping it.

    Args:
        db: Active database session (caller controls the transaction).
        year: Summer League season year.
        league_id: NBA Stats LeagueID for the venue.
        phase: Which batched normalization phase this batch covered.
        game_ids: The game IDs this batch just committed. A no-op when empty.
        now: Override for the completion timestamp (tests).
    """
    ids = list(game_ids)
    if not ids:
        return
    completed_at = now or datetime.utcnow()
    stmt = insert(_TABLE).values(
        [
            {
                "year": year,
                "league_id": league_id,
                "phase": phase,
                "game_id": game_id,
                "completed_at": completed_at,
            }
            for game_id in ids
        ]
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["year", "league_id", "phase", "game_id"]
    )
    await db.execute(stmt)
    await db.flush()
