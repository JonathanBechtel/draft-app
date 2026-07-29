"""Compact superseded intra-day Summer League metric projections.

Metric rebuilds are append-only so readers can keep using the last coherent
published version while a new projection is staged. The resulting history is
valuable, but hourly rebuilds during an event are operational churn rather than
analytical granularity. This module keeps the latest inactive projection for
each scope and UTC source day, plus every current row, and removes only older
closed-day duplicates.

Compaction is intentionally separate from the rebuild path. It runs in its
own short transaction under the shared Summer League writer lock, so the
expensive compute/materialization path never waits for or performs retention
work. The daily winner is selected from inactive rows too: an uncommitted
candidate that is the newest version for its source day remains present while
the rebuild is in flight and can still be published afterward.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.summer_league_metrics import (
    SummerLeagueMetricContext,
    SummerLeaguePlayerSeason,
)
from app.services.summer_league.write_lock import acquire_summer_league_writer_lock


@dataclass(frozen=True)
class MetricCompactionSummary:
    """Rows removed by one compaction run and the closed-day boundary used."""

    cutoff: datetime
    context_rows_deleted: int
    season_rows_deleted: int

    @property
    def rows_deleted(self) -> int:
        """Return the total number of projection rows removed."""
        return self.context_rows_deleted + self.season_rows_deleted


def _utc_naive(value: datetime) -> datetime:
    """Normalize an input timestamp to the naive UTC convention used by the schema."""
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _closed_day_cutoff(now: datetime | None) -> datetime:
    """Return midnight UTC; rows on the current source day are not compacted."""
    current = _utc_naive(now or datetime.now(timezone.utc))
    return datetime.combine(current.date(), time.min)


async def _delete_superseded_closed_day_rows(
    db: AsyncSession,
    *,
    model: Any,
    scope_columns: tuple[Any, ...],
    cutoff: datetime,
) -> int:
    """Delete non-current rows except the latest version in each closed source day."""
    source_day = func.date_trunc("day", model.as_of)
    ranked = (
        select(
            model.id.label("row_id"),
            func.row_number()
            .over(
                partition_by=(*scope_columns, source_day),
                order_by=(model.version.desc(), model.id.desc()),
            )
            .label("daily_rank"),
        )
        .where(
            model.is_current.is_(False),
            model.as_of.isnot(None),
            model.as_of < cutoff,
        )
        .cte("closed_day_rows")
    )
    # The predicate is deliberately scoped by primary key. Besides making the
    # delete safe, this keeps the repository's unscoped-delete guard meaningful.
    statement = delete(model).where(
        model.id.in_(select(ranked.c.row_id).where(ranked.c.daily_rank > 1))
    )
    result = await db.execute(statement)
    return int(result.rowcount or 0)


async def compact_metric_versions(
    db: AsyncSession,
    *,
    now: datetime | None = None,
) -> MetricCompactionSummary:
    """Compact closed-day metric versions while preserving current and daily rows.

    as_of is source currency, so the UTC calendar day of that column—not
    process time or row creation time—defines a daily history point. The
    current UTC day is left untouched because its final version is not known
    until the day closes. Rows with no as_of value are also retained: they
    predate dated publication and cannot be safely assigned to a day.

    The caller owns the transaction. This function acquires the same
    transaction-scoped writer lock used by metric publication before issuing
    either delete, so a pointer flip and compaction cannot interleave.

    Args:
        db: Active database session.
        now: Optional clock value, primarily for deterministic backfills/tests.

    Returns:
        Counts of deleted context and player-season rows plus the cutoff.
    """
    cutoff = _closed_day_cutoff(now)
    await acquire_summer_league_writer_lock(db)
    context_rows_deleted = await _delete_superseded_closed_day_rows(
        db,
        model=SummerLeagueMetricContext,
        scope_columns=(SummerLeagueMetricContext.competition_id,),
        cutoff=cutoff,
    )
    season_rows_deleted = await _delete_superseded_closed_day_rows(
        db,
        model=SummerLeaguePlayerSeason,
        scope_columns=(
            SummerLeaguePlayerSeason.competition_id,
            SummerLeaguePlayerSeason.player_id,
        ),
        cutoff=cutoff,
    )
    return MetricCompactionSummary(
        cutoff=cutoff,
        context_rows_deleted=context_rows_deleted,
        season_rows_deleted=season_rows_deleted,
    )
