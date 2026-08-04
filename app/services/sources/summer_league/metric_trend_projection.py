"""Materialize daily cohort bands beside Summer League metric projections."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from math import floor
from typing import Any, TypeAlias

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.summer_league_metrics import SummerLeagueDerivedAgg
from app.services.stats.inputs import PlayerSeason

TREND_METRIC_KEYS = ("gmsc", "ts_pct", "bpm")
TrendBand: TypeAlias = dict[str, float]
TrendBands: TypeAlias = dict[str, TrendBand]


@dataclass(frozen=True)
class ScopedSeasonTrendProjection:
    """A merged scoped band and the oldest snapshot watermark it depends on."""

    bands: dict[int, TrendBands]
    as_of: datetime | None


def percentile(values: Sequence[float], probability: float) -> float:
    """Return a deterministic linear-interpolated percentile."""
    ordered = sorted(values)
    if not ordered:
        raise ValueError("percentile requires at least one value")
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = floor(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _bands_for_seasons(seasons: Sequence[PlayerSeason]) -> TrendBands:
    """Compute the supported metric bands for one already-bounded cohort."""
    values_by_metric: dict[str, list[float]] = defaultdict(list)
    for metric_key in TREND_METRIC_KEYS:
        values_by_metric[metric_key].extend(
            float(value)
            for season in seasons
            if (value := season.metrics.get(metric_key)) is not None
        )
    return _bands_for_values(values_by_metric)


def _bands_for_values(values_by_metric: dict[str, list[float]]) -> TrendBands:
    """Compute bands from already grouped metric values."""
    bands: TrendBands = {}
    for metric_key, values in values_by_metric.items():
        if values:
            bands[metric_key] = {
                "median": percentile(values, 0.5),
                "q1": percentile(values, 0.25),
                "q3": percentile(values, 0.75),
            }
    return bands


async def materialize_scoped_season_trend_bands(
    db: AsyncSession,
    seasons: Sequence[PlayerSeason],
    *,
    scoped_competition_ids: frozenset[int],
    scoped_as_of: datetime | None,
) -> ScopedSeasonTrendProjection:
    """Merge a scoped tick with other current competitions in the same years."""
    years = {season.year for season in seasons}
    values_by_year: dict[int, dict[str, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    sibling_watermarks: list[datetime | None] = []
    if years:
        rows = (
            await db.execute(
                select(  # type: ignore[call-overload]
                    SummerLeagueDerivedAgg.year,
                    SummerLeagueDerivedAgg.gmsc,
                    SummerLeagueDerivedAgg.ts_pct,
                    SummerLeagueDerivedAgg.bpm,
                    SummerLeagueDerivedAgg.as_of,
                ).where(
                    SummerLeagueDerivedAgg.is_current.is_(True),  # type: ignore[attr-defined]
                    SummerLeagueDerivedAgg.year.in_(years),  # type: ignore[attr-defined]
                    SummerLeagueDerivedAgg.competition_id.not_in(  # type: ignore[attr-defined]
                        scoped_competition_ids
                    ),
                )
            )
        ).all()
        sibling_watermarks = [row.as_of for row in rows]
        for row in rows:
            for metric_key in TREND_METRIC_KEYS:
                value: Any = getattr(row, metric_key)
                if value is not None:
                    values_by_year[int(row.year)][metric_key].append(float(value))
    for season in seasons:
        for metric_key in TREND_METRIC_KEYS:
            value = season.metrics.get(metric_key)
            if value is not None:
                values_by_year[season.year][metric_key].append(float(value))
    watermarks = [scoped_as_of, *sibling_watermarks]
    known_watermarks = [value for value in watermarks if value is not None]
    conservative_as_of = (
        min(known_watermarks)
        if watermarks and len(known_watermarks) == len(watermarks)
        else None
    )
    return ScopedSeasonTrendProjection(
        bands={
            year: _bands_for_values(metric_values)
            for year, metric_values in values_by_year.items()
        },
        as_of=conservative_as_of,
    )


def materialize_trend_bands(
    seasons: Sequence[PlayerSeason],
    *,
    include_season_scope: bool,
) -> tuple[dict[int, TrendBands], dict[int, TrendBands]]:
    """Build competition and optional season bands during offline projection."""
    by_competition: dict[int, list[PlayerSeason]] = defaultdict(list)
    by_year: dict[int, list[PlayerSeason]] = defaultdict(list)
    for season in seasons:
        by_competition[season.competition_id].append(season)
        if include_season_scope:
            by_year[season.year].append(season)
    return (
        {key: _bands_for_seasons(rows) for key, rows in by_competition.items()},
        {key: _bands_for_seasons(rows) for key, rows in by_year.items()},
    )
