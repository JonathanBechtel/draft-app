"""Read the retained daily Summer League metric trend projection.

The materialized player-season rows are versioned by competition.  This module
is deliberately agnostic about the event's concrete table names at its public
seam: callers provide a stable ``scope_key`` and registry metric keys, while
the implementation resolves those keys to the persisted projection columns.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from math import floor
from typing import Any

from sqlalchemy import Date, and_, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.summer_league_trends import TrendCohortBand, TrendPoint
from app.schemas.summer_league_metrics import SummerLeaguePlayerSeason
from app.services.stats.registry import get_metric

TREND_METRIC_LABELS: dict[str, str] = {
    "gmsc": "GmSc",
    "ts_pct": "TS%",
    "bpm": "BPM",
}


def _scope_filter(scope_key: str) -> tuple[str, int]:
    """Parse a stable scope key into its kind and numeric value."""
    kind, separator, raw_value = scope_key.partition(":")
    if not separator or kind not in {"competition", "season"}:
        raise ValueError(
            "scope_key must be a stable 'competition:<id>' or 'season:<year>' key"
        )
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"scope_key has a non-numeric value: {scope_key!r}") from exc
    if value < 1:
        raise ValueError(f"scope_key value must be positive: {scope_key!r}")
    return kind, value


def _validate_metric_keys(metric_keys: Sequence[str]) -> tuple[str, ...]:
    """Validate registry membership and persisted-column support once at the boundary."""
    keys = tuple(dict.fromkeys(metric_keys))
    if not keys:
        raise ValueError("metric_keys must contain at least one registry key")
    for key in keys:
        try:
            get_metric(key)
        except KeyError as exc:
            raise ValueError(f"unknown registry metric key: {key!r}") from exc
        if not hasattr(SummerLeaguePlayerSeason, key):
            raise ValueError(
                f"registry metric key {key!r} is not available in the trend projection"
            )
    return keys


def _legacy_effective_day() -> Any:
    """Return the Eastern calendar date of a legacy publication timestamp."""
    # Stored timestamps are naive UTC.  ``timezone('UTC', timestamp)`` attaches
    # the source zone and the outer call converts to an Eastern wall-clock date.
    return cast(
        func.timezone(
            "America/New_York",
            func.timezone("UTC", SummerLeaguePlayerSeason.published_at),
        ),
        Date,
    )


def _effective_day_expression() -> Any:
    """Coalesce the explicit event day with the legacy publish-date fallback."""
    return func.coalesce(
        SummerLeaguePlayerSeason.effective_day,
        _legacy_effective_day(),
    )


def _percentile(values: Sequence[float], probability: float) -> float:
    """Compute a deterministic linear-interpolated percentile for a cohort."""
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = floor(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


async def get_daily_trend(  # noqa: C901
    db: AsyncSession,
    *,
    scope_key: str,
    player_id: int | None,
    metric_keys: Sequence[str],
    event_window: tuple[date, date] | None = None,
) -> list[TrendPoint]:
    """Return latest published daily-close points for a scope.

    One version is selected for each requested scope/event-day partition (a
    competition or the whole season), then all player rows from that daily
    close are returned. ``published_at`` is the visibility gate and tie-breaker only;
    the event-day expression (never ``as_of``) supplies ordering.  For a player
    request, ``value`` is that player's projection while the cohort band uses
    every player in the scope on the same daily close.  A scope request without
    ``player_id`` returns the cohort median as its value.

    Args:
        db: Active async session; callers own the transaction.
        scope_key: ``competition:<id>`` or ``season:<year>``.
        player_id: Optional player to isolate within the cohort.
        metric_keys: Registry metric keys that are persisted on the projection.
        event_window: Optional inclusive ``(start_day, end_day)`` filter.
    """
    scope_kind, scope_value = _scope_filter(scope_key)
    keys = _validate_metric_keys(metric_keys)

    effective_day = _effective_day_expression()
    competition_id_column: Any = getattr(SummerLeaguePlayerSeason, "competition_id")
    player_id_column: Any = getattr(SummerLeaguePlayerSeason, "player_id")
    year_column: Any = getattr(SummerLeaguePlayerSeason, "year")
    version_column: Any = getattr(SummerLeaguePlayerSeason, "version")
    published_at_column: Any = getattr(SummerLeaguePlayerSeason, "published_at")
    id_column: Any = getattr(SummerLeaguePlayerSeason, "id")
    selected_columns = [
        id_column,
        competition_id_column,
        player_id_column,
        version_column,
        getattr(SummerLeaguePlayerSeason, "as_of"),
        published_at_column,
        effective_day.label("effective_day"),
        *[getattr(SummerLeaguePlayerSeason, key).label(key) for key in keys],
    ]
    winner_partition = (
        (competition_id_column, effective_day)
        if scope_kind == "competition"
        else (effective_day,)
    )
    rank = func.row_number().over(
        partition_by=winner_partition,
        order_by=(
            version_column.desc(),
            published_at_column.desc(),
            id_column.desc(),
        ),
    )
    query = select(*selected_columns, rank.label("daily_rank")).where(
        published_at_column.is_not(None)
    )
    if scope_kind == "competition":
        query = query.where(competition_id_column == scope_value)
    else:
        query = query.where(year_column == scope_value)
    if event_window is not None:
        start_day, end_day = event_window
        if end_day < start_day:
            raise ValueError("event_window end must not precede its start")
        query = query.where(effective_day.between(start_day, end_day))

    ranked = query.cte("daily_metric_versions")
    # Pick exactly one published version for each competition/day before
    # returning any player rows. A partial later version must not be combined
    # with players left behind in an older version: the cohort and player value
    # are read from the same daily-close snapshot.
    winners = (
        select(
            ranked.c.competition_id,
            ranked.c.effective_day,
            ranked.c.version,
        )
        .where(ranked.c.daily_rank == 1)
        .cte("latest_daily_metric_versions")
    )
    join_conditions = [
        ranked.c.effective_day == winners.c.effective_day,
        ranked.c.version == winners.c.version,
    ]
    if scope_kind == "competition":
        join_conditions.append(ranked.c.competition_id == winners.c.competition_id)
    latest = ranked.join(winners, and_(*join_conditions))
    rows = (
        (
            await db.execute(
                select(ranked)
                .select_from(latest)
                .order_by(ranked.c.effective_day, ranked.c.player_id)
            )
        )
        .mappings()
        .all()
    )

    by_day: dict[date, list[Any]] = {}
    for row in rows:
        day = row["effective_day"]
        if day is not None:
            by_day.setdefault(day, []).append(row)

    points: list[TrendPoint] = []
    metric_order = {key: index for index, key in enumerate(keys)}
    for day in sorted(by_day):
        cohort_rows = by_day[day]
        for key in keys:
            values = [float(row[key]) for row in cohort_rows if row[key] is not None]
            if not values:
                continue
            band = TrendCohortBand(
                median=_percentile(values, 0.5),
                q1=_percentile(values, 0.25),
                q3=_percentile(values, 0.75),
            )
            target = (
                next(
                    (
                        row
                        for row in cohort_rows
                        if row["player_id"] == player_id and row[key] is not None
                    ),
                    None,
                )
                if player_id is not None
                else None
            )
            if player_id is not None and target is None:
                continue
            value = float(target[key]) if target is not None else band.median
            as_of_values = (
                [target["as_of"]]
                if target is not None and target["as_of"] is not None
                else [row["as_of"] for row in cohort_rows if row["as_of"] is not None]
            )
            points.append(
                TrendPoint(
                    metric_key=key,
                    effective_day=day,
                    value=value,
                    cohort_band=band,
                    as_of=max(as_of_values) if as_of_values else None,
                )
            )

    # The loops above already produce day order. Keep the explicit key order in
    # the contract even when the database returns rows in a different plan order.
    points.sort(key=lambda point: (point.effective_day, metric_order[point.metric_key]))
    return points


def trend_points_to_context(
    points: Sequence[TrendPoint],
    *,
    scope_key: str,
    scope_label: str,
    player_id: int | None = None,
) -> dict[str, Any] | None:
    """Build the small JSON contract consumed by the trend chart component.

    The read service owns ordering and daily-close selection; this adapter only
    serializes the already-selected points and computes display metadata.  It is
    deliberately scope/event-parameterized so the same component can be reused
    by another event spoke without changing its template or JavaScript contract.
    """
    if not points:
        return None
    ordered = sorted(points, key=lambda point: (point.effective_day, point.metric_key))
    metric_keys = [
        key for key in TREND_METRIC_LABELS if any(p.metric_key == key for p in ordered)
    ]
    if not metric_keys:
        metric_keys = list(dict.fromkeys(p.metric_key for p in ordered))
    as_of_values = [point.as_of for point in ordered if point.as_of is not None]
    latest_day = max(point.effective_day for point in ordered)
    latest_as_of = max(as_of_values) if as_of_values else None
    return {
        "scope_key": scope_key,
        "scope_label": scope_label,
        "player_id": player_id,
        "metric_keys": metric_keys,
        "metrics": [
            {"key": key, "label": TREND_METRIC_LABELS.get(key, key.upper())}
            for key in metric_keys
        ],
        "points": [point.model_dump(mode="json") for point in ordered],
        "latest_effective_day": latest_day.isoformat(),
        "latest_as_of": latest_as_of.isoformat() if latest_as_of else None,
        "single_point": len({point.effective_day for point in ordered}) == 1,
    }
