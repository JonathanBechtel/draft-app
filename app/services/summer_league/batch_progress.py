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

from sqlalchemy import delete, select
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


async def invalidate_batch_progress(
    db: AsyncSession,
    *,
    year: int,
    league_id: str,
    phase: SummerLeagueBatchPhase,
    game_ids: Iterable[str] | None = None,
) -> None:
    """Delete durable batch-progress rows so the covered games reprocess.

    ``SummerLeagueBatchProgress`` rows are otherwise permanent -- that is
    what makes a routine run against an unchanged venue cheap (see
    :func:`get_completed_batch_game_ids`), but it also means a game whose
    raw snapshot changes *after* its batch already committed (a forced
    re-fetch correcting a bad box score, or any future path that rewrites an
    already-normalized game's file) would otherwise be skipped forever.
    Deleting its progress row here is what lets it re-enter
    :func:`~app.cli.summer_league_ingest_runner._run_batched_phase`'s
    ordinary ``remaining`` filter on the very next call, with no change
    needed to that filter itself.

    Args:
        db: Active database session (caller controls the transaction).
        year: Summer League season year.
        league_id: NBA Stats LeagueID for the venue.
        phase: Which batched normalization phase to invalidate.
        game_ids: The specific game IDs to invalidate (e.g. this run's dirty
            set -- see
            ``app.services.summer_league.raw_ingestion.dirty_game_ids_from_manifest``).
            ``None`` deletes every row for this ``year``/``league_id``/
            ``phase`` outright -- the full-reconciliation/repair mode
            (``SL_INGEST_FULL_RECONCILE``). An empty iterable is a no-op,
            mirroring :func:`record_batch_progress`.
    """
    conditions = [
        _TABLE.c.year == year,
        _TABLE.c.league_id == league_id,
        _TABLE.c.phase == phase,
    ]
    if game_ids is not None:
        ids = list(game_ids)
        if not ids:
            return
        conditions.append(_TABLE.c.game_id.in_(ids))
    await db.execute(delete(_TABLE).where(*conditions))
    await db.flush()


async def count_pending_batch_games(
    db: AsyncSession,
    *,
    year: int,
    league_id: str,
    phase: SummerLeagueBatchPhase,
    discovered_game_ids: Iterable[str],
) -> int:
    """Count discovered games not yet durably completed for one phase/venue/year.

    The "dirty-game backlog" this run has left outstanding: games this run's
    raw fetch discovered (``discovered_game_ids``, e.g. a venue's
    ``game_ids_universe`` in ``app.cli.summer_league_ingest_runner``) that
    have not yet committed a batch for ``phase``. Modeled on
    :func:`~app.services.summer_league.normalization.find_incomplete_team_box_game_ids`'s
    "a query that answers how many games still need another pass" shape, but
    scoped to batch-progress state rather than a new durable table -- a
    later operational-telemetry ticket surfaces this as a metric.

    Args:
        db: Active database session.
        year: Summer League season year.
        league_id: NBA Stats LeagueID for the venue.
        phase: Which batched normalization phase to check.
        discovered_game_ids: The full set of game IDs known to exist for
            this venue/year (not just this run's dirty set).

    Returns:
        The number of ``discovered_game_ids`` not yet in the completed set.
        ``0`` when ``discovered_game_ids`` is empty or every discovered game
        is already complete.
    """
    discovered = set(discovered_game_ids)
    if not discovered:
        return 0
    completed = await get_completed_batch_game_ids(
        db, year=year, league_id=league_id, phase=phase
    )
    return len(discovered - completed)
