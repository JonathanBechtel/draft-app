"""Materialize daily cohort bands beside Summer League metric projections."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from math import floor
from typing import TypeAlias

from app.services.stats.inputs import PlayerSeason

TREND_METRIC_KEYS = ("gmsc", "ts_pct", "bpm")
TrendBand: TypeAlias = dict[str, float]
TrendBands: TypeAlias = dict[str, TrendBand]


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
    bands: TrendBands = {}
    for metric_key in TREND_METRIC_KEYS:
        values = [
            float(value)
            for season in seasons
            if (value := season.metrics.get(metric_key)) is not None
        ]
        if values:
            bands[metric_key] = {
                "median": percentile(values, 0.5),
                "q1": percentile(values, 0.25),
                "q3": percentile(values, 0.75),
            }
    return bands


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
