"""Read the retained daily Summer League metric trend projection.

The materialized player-season rows are versioned by competition.  This module
is deliberately agnostic about the event's concrete table names at its public
seam: callers provide a stable ``scope_key`` and registry metric keys, while
the implementation resolves those keys to the persisted projection columns.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.temporal import Watermark
from app.models.summer_league_trends import TrendCohortBand, TrendPoint
from app.schemas.summer_league import SummerLeagueEdition
from app.schemas.summer_league_metrics import SummerLeaguePlayerSeason
from app.services.stats.registry import get_metric

TREND_METRIC_LABELS: dict[str, str] = {
    "gmsc": "GmSc",
    "ts_pct": "TS%",
    "bpm": "BPM",
}
TREND_METRIC_KEYS = ("gmsc", "ts_pct", "bpm")

#: Rendering format for source currency, shared with the Explorer's freshness
#: labels (``_explorer_results.html``) so one surface never reads as an ISO
#: timestamp while its sibling reads as a human label.
AS_OF_LABEL_FORMAT = "%Y-%m-%d %H:%M UTC"

#: Shown when a scope's newest daily close has no reported source watermark;
#: matches the Explorer's wording for the same unknown state.
AS_OF_UNKNOWN_LABEL = "not reported"

#: Last-resort scope label. A scope key is an internal database identifier and
#: must never surface to a reader, least of all on a shared PNG.
FALLBACK_SCOPE_LABEL = "Summer League"


def latest_trend_watermark(points: Sequence[TrendPoint]) -> Watermark:
    """Return the freshness contract for the newest day without masking unknown data.

    ``source_as_of`` is the max per-point source currency for the latest
    ``effective_day``, or ``None`` when any point on that day cannot state its
    currency -- P4 requires the gap stay visible rather than being papered over
    with a sibling point's value. The trend projection does not currently carry
    a build timestamp or a projection-version counter, so
    ``projection_built_at``/``projection_version`` are ``None`` here; adding them
    is a follow-up to this read path, not a value this seam can invent.
    """
    if not points:
        return Watermark(
            source_as_of=None, projection_built_at=None, projection_version=None
        )
    latest_day = max(point.effective_day for point in points)
    latest_values = [
        point.as_of for point in points if point.effective_day == latest_day
    ]
    if not latest_values or any(value is None for value in latest_values):
        return Watermark(
            source_as_of=None, projection_built_at=None, projection_version=None
        )
    return Watermark(
        source_as_of=max(value for value in latest_values if value is not None),
        projection_built_at=None,
        projection_version=None,
    )


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


async def resolve_scope_label(db: AsyncSession, scope_key: str) -> str:
    """Return a reader-facing label for a stable scope key.

    Scope keys are internal identifiers (``competition:42``). Any surface a
    person reads -- and especially a share card that leaves the site -- must
    show the event's own name instead. The label is resolved server-side from
    the canonical competition row rather than accepted from a caller, so an
    export request can never inject arbitrary text into a rendered PNG.

    Args:
        db: Active async session; the caller owns the transaction.
        scope_key: ``competition:<id>`` or ``season:<year>``.

    Returns:
        The competition's display name, a season label, or a neutral fallback
        when the scope has no canonical row.
    """
    scope_kind, scope_value = _scope_filter(scope_key)
    if scope_kind == "season":
        return f"{scope_value} Summer League"
    display_name_column: Any = getattr(SummerLeagueEdition, "display_name")
    id_column: Any = getattr(SummerLeagueEdition, "id")
    display_name = (
        await db.execute(select(display_name_column).where(id_column == scope_value))
    ).scalar_one_or_none()
    return str(display_name) if display_name else FALLBACK_SCOPE_LABEL


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


def _effective_day_expression() -> Any:
    """Return the explicit event day; job timestamps are not event evidence."""
    return SummerLeaguePlayerSeason.effective_day


def _shape_trend_points(
    rows: Sequence[Any],
    *,
    scope_kind: str,
    keys: Sequence[str],
    player_id: int | None,
) -> list[TrendPoint]:
    """Shape one materialized row per day into the public point contract."""
    points: list[TrendPoint] = []
    metric_order = {key: index for index, key in enumerate(keys)}
    band_column = (
        "trend_competition_bands"
        if scope_kind == "competition"
        else "trend_season_bands"
    )
    as_of_column = "as_of" if scope_kind == "competition" else "trend_season_as_of"
    for row in rows:
        day = row["effective_day"]
        if day is None:
            continue
        stored_bands = row[band_column] or {}
        for key in keys:
            stored_band = stored_bands.get(key)
            if stored_band is None or (player_id is not None and row[key] is None):
                continue
            band = TrendCohortBand(
                median=float(stored_band["median"]),
                q1=float(stored_band["q1"]),
                q3=float(stored_band["q3"]),
            )
            points.append(
                TrendPoint(
                    metric_key=key,
                    effective_day=day,
                    value=float(row[key]) if player_id is not None else band.median,
                    cohort_band=band,
                    as_of=row[as_of_column],
                )
            )
    points.sort(key=lambda point: (point.effective_day, metric_order[point.metric_key]))
    return points


async def get_daily_trend(
    db: AsyncSession,
    *,
    scope_key: str,
    player_id: int | None,
    metric_keys: Sequence[str],
    event_window: tuple[date, date] | None = None,
) -> list[TrendPoint]:
    """Return latest published daily-close points for a scope.

    One version is selected for each competition/event-day partition inside the
    requested scope, then all player rows from those daily closes are returned.
    A season scope therefore combines the latest close from every competition
    sharing an event day (the archival publisher writes one competition/day at a
    time). ``published_at`` is the visibility gate and tie-breaker only;
    the event-day expression (never ``as_of``) supplies ordering.  For a player
    request, ``value`` is that player's projection while the cohort band comes
    from the offline materialized projection. A scope request without
    ``player_id`` returns the stored cohort median as its value. The request path
    reads at most one projection row per event day and never rebuilds quartiles.

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
    archival_column: Any = getattr(SummerLeaguePlayerSeason, "is_archival")
    selected_columns = [
        id_column,
        competition_id_column,
        player_id_column,
        version_column,
        getattr(SummerLeaguePlayerSeason, "as_of"),
        published_at_column,
        effective_day.label("effective_day"),
        archival_column,
        getattr(SummerLeaguePlayerSeason, "trend_competition_bands"),
        getattr(SummerLeaguePlayerSeason, "trend_season_bands"),
        getattr(SummerLeaguePlayerSeason, "trend_season_as_of"),
        *[getattr(SummerLeaguePlayerSeason, key).label(key) for key in keys],
    ]
    # Publication versions are allocated globally, while the archival backfill
    # publishes each competition/day in its own candidate version. Partitioning
    # a season only by ``effective_day`` would consequently retain whichever
    # competition happened to receive the highest global version and silently
    # omit all sibling competitions on that day. Keep the competition dimension
    # in the winner key for both scope kinds; season rows are combined below.
    winner_partition = (competition_id_column, effective_day)
    rank = func.row_number().over(
        partition_by=winner_partition,
        order_by=(
            archival_column.desc(),
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
    join_conditions.append(ranked.c.competition_id == winners.c.competition_id)
    latest = ranked.join(winners, and_(*join_conditions))
    candidates = select(ranked).select_from(latest)
    if player_id is not None:
        candidates = candidates.where(ranked.c.player_id == player_id)
    # A player can appear in sibling competitions on the same day. Choose one
    # deterministic row, and likewise choose one representative row for a
    # scope-median request; each row carries the same materialized scope band.
    scope_rank = func.row_number().over(
        partition_by=ranked.c.effective_day,
        order_by=(
            ranked.c.is_archival.desc(),
            ranked.c.version.desc(),
            ranked.c.published_at.desc(),
            ranked.c.id.desc(),
        ),
    )
    daily_candidates = candidates.add_columns(scope_rank.label("scope_rank")).cte(
        "daily_trend_candidates"
    )
    rows = (
        (
            await db.execute(
                select(daily_candidates)
                .where(daily_candidates.c.scope_rank == 1)
                .order_by(daily_candidates.c.effective_day)
            )
        )
        .mappings()
        .all()
    )

    return _shape_trend_points(
        rows,
        scope_kind=scope_kind,
        keys=keys,
        player_id=player_id,
    )


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
    latest_day = max(point.effective_day for point in ordered)
    latest_as_of = latest_trend_watermark(ordered).source_as_of
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
        # The ISO value stays machine-readable (``<time datetime>``, clients);
        # the label is what a reader sees, formatted like the Explorer.
        "latest_as_of": latest_as_of.isoformat() if latest_as_of else None,
        "latest_as_of_label": (
            latest_as_of.strftime(AS_OF_LABEL_FORMAT)
            if latest_as_of
            else AS_OF_UNKNOWN_LABEL
        ),
        "single_point": len({point.effective_day for point in ordered}) == 1,
    }


async def build_trend_context(
    db: AsyncSession,
    *,
    scope_key: str,
    scope_label: str,
    player_id: int | None = None,
) -> dict[str, Any] | None:
    """Fetch the standard trend metrics and serialize one page context."""
    points = await get_daily_trend(
        db,
        scope_key=scope_key,
        player_id=player_id,
        metric_keys=TREND_METRIC_KEYS,
    )
    return trend_points_to_context(
        points,
        scope_key=scope_key,
        scope_label=scope_label,
        player_id=player_id,
    )


async def build_player_trend_context(
    db: AsyncSession,
    *,
    player_id: int,
    year: int,
    venue_slug: str | None,
) -> dict[str, Any] | None:
    """Resolve a player's concrete event and build its trend context."""
    from app.services.summer_league_stats_service import (
        get_competition_id_for_player_year,
    )

    competition_id = await get_competition_id_for_player_year(
        db,
        player_id=player_id,
        year=year,
        venue_slug=venue_slug,
    )
    if competition_id is None:
        return None
    return await build_trend_context(
        db,
        scope_key=f"competition:{competition_id}",
        scope_label=f"{year} trend",
        player_id=player_id,
    )
