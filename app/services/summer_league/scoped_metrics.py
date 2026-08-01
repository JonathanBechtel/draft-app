"""Fit/projection split for the Summer League metrics orchestration.

The pooled fit is deliberately separate from the competition projection. Full
rebuilds produce the fit; in-event scoped rebuilds reuse it and only assemble
the selected competition's source rows.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from app.services.stats.inputs import (
    BOX_INT_FIELDS,
    PlayerSeason,
    PoolContext,
    StatInputs,
)


@dataclass(frozen=True)
class MetricFit:
    """Pooled coefficients reused by scoped competition projections."""

    pyth_exponent: float
    pyth_n: int
    ws_ppw_coeff: float
    bpm_coef: Optional[dict[str, float]]
    bpm_intercept: float
    bpm_r2: float
    bpm_n_fit: int
    model_version: Optional[str] = None


@dataclass
class ComputeResult:
    """A pooled fit plus the projections produced from it."""

    fit: MetricFit
    contexts: dict[int, PoolContext]
    seasons: list[PlayerSeason]
    shot_diet: dict[tuple[int, int], dict[str, int]] = field(default_factory=dict)
    assisted_fg: dict[tuple[int, int], tuple[int, int]] = field(default_factory=dict)
    as_of: Optional[datetime] = None

    @property
    def pyth_exponent(self) -> float:
        """Return the fit's Pythagorean exponent for legacy callers."""
        return self.fit.pyth_exponent

    @property
    def pyth_n(self) -> int:
        """Return the fit's Pythagorean sample size for legacy callers."""
        return self.fit.pyth_n

    @property
    def ws_ppw_coeff(self) -> float:
        """Return the fit's Win Shares points-per-win coefficient."""
        return self.fit.ws_ppw_coeff

    @property
    def bpm_coef(self) -> Optional[dict[str, float]]:
        """Return the fit's BPM coefficient vector for legacy callers."""
        return self.fit.bpm_coef

    @property
    def bpm_intercept(self) -> float:
        """Return the fit's BPM intercept for legacy callers."""
        return self.fit.bpm_intercept

    @property
    def bpm_r2(self) -> float:
        """Return the fit's BPM R-squared value for legacy callers."""
        return self.fit.bpm_r2

    @property
    def bpm_n_fit(self) -> int:
        """Return the fit's BPM sample size for legacy callers."""
        return self.fit.bpm_n_fit


def assemble_seasons(
    comps: Mapping[int, tuple[int, str]],
    team_box: Mapping[int, StatInputs],
    opp_box: Mapping[int, StatInputs],
    player_rows: Sequence[Any],
    source_rate_columns: Mapping[str, str],
) -> list[PlayerSeason]:
    """Merge player rows and attach the selected team's opponent context."""
    merged: dict[tuple[int, int], dict[str, Any]] = {}
    for row in player_rows:
        key = (row.competition_id, row.player_id)
        seconds = float(row.sec or 0)
        current = merged.get(key)
        if current is None:
            current = {
                "box": StatInputs(),
                "entry": row.team_entry_id,
                "sec": seconds,
                "pm": 0.0,
                "source_rate_weighted": defaultdict(float),
                "source_rate_seconds": defaultdict(float),
            }
            merged[key] = current
        box = current["box"]
        box.gp += int(row.gp)
        box.mp += seconds / 60.0
        current["pm"] += float(row.plus_minus or 0)
        for field_name in BOX_INT_FIELDS:
            setattr(
                box,
                field_name,
                getattr(box, field_name) + float(getattr(row, field_name) or 0),
            )
        for metric in source_rate_columns:
            current["source_rate_weighted"][metric] += float(
                getattr(row, f"{metric}_weighted") or 0
            )
            current["source_rate_seconds"][metric] += float(
                getattr(row, f"{metric}_seconds") or 0
            )
        if seconds > current["sec"]:
            current["entry"] = row.team_entry_id
            current["sec"] = seconds

    seasons: list[PlayerSeason] = []
    for (competition_id, player_id), value in merged.items():
        year, venue = comps[competition_id]
        source_rates = {
            metric: (
                round(
                    100.0 * value["source_rate_weighted"][metric] / denominator,
                    1,
                )
                if (denominator := value["source_rate_seconds"][metric])
                else None
            )
            for metric in source_rate_columns
        }
        entry = value["entry"]
        seasons.append(
            PlayerSeason(
                player_id=player_id,
                competition_id=competition_id,
                primary_team_entry_id=entry,
                year=year,
                venue=venue,
                box=value["box"],
                team=team_box[entry],
                opp=opp_box[entry],
                pm=value["pm"],
                source_rates=source_rates,
            )
        )
    return seasons


def project_metrics(
    seasons: list[PlayerSeason],
    contexts: dict[int, PoolContext],
    records: dict[int, dict],
    team_comp: dict[int, int],
    pooled_fit: Optional[MetricFit],
) -> MetricFit:
    """Apply a full or reusable fit to the loaded competition projections."""
    # Imports stay local: metrics.py owns the SL-native fit implementations and
    # imports this module for the result types, so a module-level import cycles.
    from app.services.summer_league.metrics import (
        ADV_MIN_COMPLETE_FRAC,
        ADV_MIN_PLAYERS,
        QUALIFY_MIN_MINUTES,
        _d,
        apply_sl_bpm,
        compute_metrics,
        fit_pythagorean,
        fit_sl_bpm,
    )

    by_pool: dict[int, list[PlayerSeason]] = defaultdict(list)
    for season in seasons:
        by_pool[season.competition_id].append(season)

    adv_pools: set[int] = set()
    for competition_id, pool in by_pool.items():
        qualified = sum(1 for player in pool if player.box.mp >= QUALIFY_MIN_MINUTES)
        context = contexts[competition_id]
        complete_fraction = _d(context.complete_games, context.total_games)
        if complete_fraction >= ADV_MIN_COMPLETE_FRAC and qualified >= ADV_MIN_PLAYERS:
            adv_pools.add(competition_id)
            context.adv_eligible = True

    if pooled_fit is None:
        pyth_exponent, pyth_n = fit_pythagorean(records, team_comp, adv_pools)
        ws_ppw_coeff = 4.0 / pyth_exponent
    else:
        pyth_exponent = pooled_fit.pyth_exponent
        pyth_n = pooled_fit.pyth_n
        ws_ppw_coeff = pooled_fit.ws_ppw_coeff

    for season in seasons:
        compute_metrics(season, contexts[season.competition_id], ws_ppw_coeff)

    for competition_id, pool in by_pool.items():
        if not contexts[competition_id].adv_eligible:
            continue
        numerator = sum((season.aper or 0.0) * season.box.mp for season in pool)
        denominator = sum(season.box.mp for season in pool)
        scalar = _d(numerator, denominator)
        contexts[competition_id].aper_scalar = scalar
        for season in pool:
            season.metrics["per"] = (
                round(season.aper * _d(15.0, scalar), 1)
                if season.aper is not None
                else None
            )

    adv_by_pool = {
        competition_id: pool
        for competition_id, pool in by_pool.items()
        if competition_id in adv_pools
    }
    if pooled_fit is None:
        coef, intercept, r2, n_fit = fit_sl_bpm(seasons, adv_pools)
    else:
        coef = pooled_fit.bpm_coef
        intercept = pooled_fit.bpm_intercept
        r2 = pooled_fit.bpm_r2
        n_fit = pooled_fit.bpm_n_fit
    apply_sl_bpm(seasons, adv_by_pool, coef, intercept)
    return MetricFit(
        pyth_exponent=pyth_exponent,
        pyth_n=pyth_n,
        ws_ppw_coeff=ws_ppw_coeff,
        bpm_coef=coef,
        bpm_intercept=intercept,
        bpm_r2=r2,
        bpm_n_fit=n_fit,
        model_version=pooled_fit.model_version if pooled_fit is not None else None,
    )
