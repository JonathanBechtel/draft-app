"""Compact superseded intra-day Summer League metric projections.

Metric rebuilds are append-only so readers can keep using the last coherent
published version while a new projection is staged. The resulting history is
valuable, but hourly rebuilds during an event are operational churn rather than
analytical granularity. This module keeps the latest published and latest
unpublished projection for each scope and Eastern event day, plus every current row,
and removes only older closed-day duplicates.

Compaction is intentionally separate from the rebuild path. It runs in its
own short transaction under the shared Summer League writer lock, so the
expensive compute/materialization path never waits for or performs retention
work. The publication state is part of the ranking: an uncommitted candidate
remains present while the rebuild is in flight, but it cannot displace the last
published daily close if it is abandoned.

Retention rationale (abandoned candidates): ``_delete_superseded_closed_day_rows``
partitions by ``(*scope_columns, source_day, publication_state)`` and keeps rank 1
of each partition, so at most one unpublished ("abandoned") candidate survives
per scope per closed event day -- alongside the one published daily close. A
candidate is "abandoned" when a rebuild staged rows for a day but a later
publish never promoted them (superseded by a newer rebuild, or the run never
reached the publish step). This is a deliberate, amended-spec trade-off, not an
oversight: growth is bounded at exactly one extra row per scope per day
regardless of how many rebuilds ran that day, which is the same order of
magnitude as the published row it sits beside and provides useful audit trail
(what almost got published, and when) at negligible storage cost. No
additional time-based sweep removes these rows; if the bound ever needs to
tighten further, add an explicit "abandoned candidates older than N days" pass
rather than folding it into the daily-rank logic here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
from typing import Any

from sqlalchemy import Date, cast, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.summer_league_metrics import (
    SummerLeagueMetricContext,
    SummerLeaguePlayerSeason,
)
from app.services.event_desk.timeutils import to_eastern_date
from app.services.summer_league.write_lock import (
    acquire_summer_league_writer_lock_bounded,
)

DEFAULT_METRIC_COMPACTION_LOCK_MAX_WAIT_SECONDS = 30.0


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
    event_day_cutoff: Any | None = None,
) -> int:
    """Delete superseded published/unpublished rows from closed source days."""
    # ``effective_day`` is the event calendar day and is the primary history
    # partition. Legacy published rows predate that column, so derive their
    # day from the publication stamp in Eastern time. ``as_of`` is source
    # currency and must never influence trend ordering/retention.
    legacy_day = cast(
        func.timezone("America/New_York", func.timezone("UTC", model.published_at)),
        Date,
    )
    source_day = func.coalesce(model.effective_day, legacy_day)
    # ``cutoff`` is normalized to UTC midnight for the existing job contract;
    # compare event days against that instant's Eastern calendar date so a run
    # just after UTC midnight does not close the still-current Eastern day.
    event_day_cutoff = event_day_cutoff or to_eastern_date(cutoff)
    publication_state = model.published_at.is_(None)
    ranked = (
        select(
            model.id.label("row_id"),
            func.row_number()
            .over(
                partition_by=(*scope_columns, source_day, publication_state),
                order_by=(model.version.desc(), model.id.desc()),
            )
            .label("daily_rank"),
        )
        .where(
            model.is_current.is_(False),
            source_day < event_day_cutoff,
        )
        .cte("closed_day_rows")
    )
    # The predicate is deliberately scoped by primary key. Besides making the
    # delete safe, this keeps the repository's unscoped-delete guard meaningful.
    statement = delete(model).where(
        model.id.in_(select(ranked.c.row_id).where(ranked.c.daily_rank > 1))
    )
    result = await db.execute(statement)
    # AsyncSession.execute is typed as Result, while DELETE returns a
    # CursorResult with rowcount at runtime.
    return int(getattr(result, "rowcount", 0) or 0)


async def compact_metric_versions(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    max_wait_seconds: float = DEFAULT_METRIC_COMPACTION_LOCK_MAX_WAIT_SECONDS,
) -> MetricCompactionSummary:
    """Compact closed-day metric versions while preserving published closes/candidates.

    ``effective_day`` is the event calendar day and defines a daily history
    point. Legacy published rows use the Eastern date of ``published_at``;
    ``as_of`` is source currency and never drives retention. The current day is
    left untouched because its final version is not known until the day closes.
    Rows with no effective day or publication stamp are retained. Within a
    closed day, the latest published row and latest unpublished candidate are
    retained independently.

    The caller owns the transaction. This function acquires the same
    transaction-scoped writer lock used by metric publication before issuing
    either delete, so a pointer flip and compaction cannot interleave.

    Args:
        db: Active database session.
        now: Optional clock value, primarily for deterministic backfills/tests.
        max_wait_seconds: Maximum time to wait for the shared writer lock.

    Returns:
        Counts of deleted context and player-season rows plus the cutoff.
    """
    reference_now = now or datetime.now(timezone.utc)
    cutoff = _closed_day_cutoff(reference_now)
    event_day_cutoff = to_eastern_date(reference_now)
    await acquire_summer_league_writer_lock_bounded(
        db, max_wait_seconds=max_wait_seconds
    )
    context_rows_deleted = await _delete_superseded_closed_day_rows(
        db,
        model=SummerLeagueMetricContext,
        scope_columns=(SummerLeagueMetricContext.competition_id,),
        cutoff=cutoff,
        event_day_cutoff=event_day_cutoff,
    )
    season_rows_deleted = await _delete_superseded_closed_day_rows(
        db,
        model=SummerLeaguePlayerSeason,
        scope_columns=(
            SummerLeaguePlayerSeason.competition_id,
            SummerLeaguePlayerSeason.player_id,
        ),
        cutoff=cutoff,
        event_day_cutoff=event_day_cutoff,
    )
    return MetricCompactionSummary(
        cutoff=cutoff,
        context_rows_deleted=context_rows_deleted,
        season_rows_deleted=season_rows_deleted,
    )
