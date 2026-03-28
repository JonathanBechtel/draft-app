"""Compute composite Combine Scores from pre-computed per-metric z-scores.

Reads z-scores from PlayerMetricValue (produced by compute_metrics), applies
weighted aggregation within categories (anthropometrics, athletic testing,
shooting) and across categories for an overall score, then stores the results
back as new PlayerMetricValue rows under a ``combine_score`` MetricSource.

Usage::

    python -m app.cli.compute_combine_scores \
        --cohort current_draft --season 2024-25 \
        [--position-matrix parent] [--min-metrics 2] \
        [--replace-run] [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, cast

import numpy as np
import pandas as pd
from sqlalchemy import select, func
from sqlmodel import delete
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.fields import (
    CohortType,
    MetricCategory,
    MetricSource,
    MetricStatistic,
)
from app.models.position_taxonomy import (
    PARENT_SCOPE_PRESET,
    PositionScopeKind,
    parents_for_scope,
    resolve_position_scope,
)
from app.schemas.metrics import MetricDefinition, MetricSnapshot, PlayerMetricValue
from app.schemas.seasons import Season
from app.utils.db_async import SessionLocal, load_schema_modules

# ---------------------------------------------------------------------------
# Weight configuration
# ---------------------------------------------------------------------------

# Per-metric weights within each category.  When a player is missing
# individual metrics the available weights are renormalized to sum to 1.0.

ANTHRO_WEIGHTS: Dict[str, float] = {
    "wingspan_in": 0.275,
    "standing_reach_in": 0.275,
    # "height" is a virtual key resolved at runtime from height_wo_shoes_in
    # (preferred) or height_w_shoes_in (fallback).
    "height": 0.18,
    "weight_lb": 0.12,
    "body_fat_pct": 0.05,
    "hand_length_in": 0.05,
    "hand_width_in": 0.05,
}

ATHLETIC_WEIGHTS: Dict[str, float] = {
    "standing_vertical_in": 0.30,
    "lane_agility_time_s": 0.25,
    "shuttle_run_s": 0.18,
    "three_quarter_sprint_s": 0.15,
    "max_vertical_in": 0.07,
    "bench_press_reps": 0.05,
}

SHOOTING_WEIGHTS: Dict[str, float] = {
    "spot_up_fg_pct": 0.20,
    "free_throw_fg_pct": 0.20,
    "three_point_star_fg_pct": 0.18,
    "three_point_side_fg_pct": 0.15,
    "off_dribble_fg_pct": 0.12,
    "midrange_star_fg_pct": 0.08,
    "midrange_side_fg_pct": 0.07,
}

# Category weights for the overall composite score.
CATEGORY_WEIGHTS: Dict[str, float] = {
    "anthropometrics": 0.40,
    "combine_performance": 0.40,
    "shooting": 0.20,
}

# Mapping from category label to the per-metric weight dict and the
# MetricCategory enum used for the resulting MetricDefinition.
CATEGORY_CONFIG: Dict[str, Tuple[Dict[str, float], MetricCategory]] = {
    "anthropometrics": (ANTHRO_WEIGHTS, MetricCategory.anthropometrics),
    "combine_performance": (ATHLETIC_WEIGHTS, MetricCategory.combine_performance),
    "shooting": (SHOOTING_WEIGHTS, MetricCategory.shooting),
}

# Metric keys for the height consolidation: prefer barefoot, fall back to shoes.
HEIGHT_PREFERRED = "height_wo_shoes_in"
HEIGHT_FALLBACK = "height_w_shoes_in"

# Mapping from MetricSource to category label (for grouping source z-scores).
SOURCE_TO_CATEGORY: Dict[MetricSource, str] = {
    MetricSource.combine_anthro: "anthropometrics",
    MetricSource.combine_agility: "combine_performance",
    MetricSource.combine_shooting: "shooting",
}

# The combine-score MetricDefinition keys we create.
SCORE_DEFINITIONS = [
    {
        "metric_key": "combine_score_anthropometrics",
        "display_name": "Combine Score — Anthropometrics",
        "category": MetricCategory.anthropometrics,
    },
    {
        "metric_key": "combine_score_athletic",
        "display_name": "Combine Score — Athletic Testing",
        "category": MetricCategory.combine_performance,
    },
    {
        "metric_key": "combine_score_shooting",
        "display_name": "Combine Score — Shooting",
        "category": MetricCategory.shooting,
    },
    {
        "metric_key": "combine_score_overall",
        "display_name": "Combine Score — Overall",
        "category": MetricCategory.combine_overall,
    },
]

# Metric key for each category score.
CATEGORY_SCORE_KEYS: Dict[str, str] = {
    "anthropometrics": "combine_score_anthropometrics",
    "combine_performance": "combine_score_athletic",
    "shooting": "combine_score_shooting",
}


# ---------------------------------------------------------------------------
# Pure computation helpers (no DB access — easy to unit-test)
# ---------------------------------------------------------------------------


def renormalize_weights(
    weights: Dict[str, float], available_keys: Set[str]
) -> Dict[str, float]:
    """Return *weights* restricted to *available_keys* and rescaled to sum to 1."""
    filtered = {k: v for k, v in weights.items() if k in available_keys}
    total = sum(filtered.values())
    if total <= 0:
        return {}
    return {k: v / total for k, v in filtered.items()}


def weighted_mean_z(
    z_scores: Dict[str, float], weights: Dict[str, float]
) -> Optional[float]:
    """Compute weighted mean z-score given matching dicts.

    Returns None if no overlapping keys.
    """
    available = set(z_scores) & set(weights)
    if not available:
        return None
    normed = renormalize_weights(weights, available)
    return sum(z_scores[k] * normed[k] for k in normed)


def resolve_height_z(player_z: Dict[str, float]) -> Optional[float]:
    """Pick the best height z-score for a player (barefoot preferred)."""
    if HEIGHT_PREFERRED in player_z:
        return player_z[HEIGHT_PREFERRED]
    if HEIGHT_FALLBACK in player_z:
        return player_z[HEIGHT_FALLBACK]
    return None


def compute_category_score(
    player_z: Dict[str, float],
    category_weights: Dict[str, float],
    category_label: str,
) -> Tuple[Optional[float], Dict[str, Any]]:
    """Compute a single category score for one player.

    Returns (weighted_mean_z_or_None, component_detail_dict).
    """
    # Build the effective z-score map for this category, handling the
    # height consolidation for anthropometrics.
    effective_z: Dict[str, float] = {}
    for key in category_weights:
        if key == "height":
            hz = resolve_height_z(player_z)
            if hz is not None:
                effective_z["height"] = hz
        elif key in player_z:
            effective_z[key] = player_z[key]

    score = weighted_mean_z(effective_z, category_weights)
    components = {k: {"z_score": round(v, 4)} for k, v in effective_z.items()}
    detail: Dict[str, Any] = {
        "components": components,
        "metric_count": len(effective_z),
        "category": category_label,
    }
    return score, detail


def compute_overall_score(
    category_scores: Dict[str, float],
    category_details: Dict[str, Dict[str, Any]],
) -> Tuple[Optional[float], Dict[str, Any]]:
    """Compute the weighted overall combine score from category scores.

    Returns (weighted_mean_z_or_None, detail_dict).
    """
    available = set(category_scores)
    normed = renormalize_weights(CATEGORY_WEIGHTS, available)
    if not normed:
        return None, {}
    score = sum(category_scores[k] * normed[k] for k in normed)
    cat_info: Dict[str, Any] = {}
    for cat_label, mean_z in category_scores.items():
        detail = category_details.get(cat_label, {})
        cat_info[cat_label] = {
            "mean_z": round(mean_z, 4),
            "weight": round(normed.get(cat_label, 0), 4),
            "metric_count": detail.get("metric_count", 0),
        }
    return score, {
        "category_scores": cat_info,
        "category_count": len(normed),
    }


def rank_and_percentile(
    scores: pd.Series,
) -> pd.DataFrame:
    """Compute rank (1-based, lower rank = better) and percentile for a Series of scores.

    Higher raw score is better.
    """
    n = len(scores)
    if n == 0:
        return pd.DataFrame(columns=["rank", "percentile"])
    sorted_vals = np.sort(scores.to_numpy())
    positions = np.searchsorted(sorted_vals, scores.to_numpy(), side="right")
    percentile = (positions / n) * 100.0
    pos_right = np.searchsorted(sorted_vals, scores.to_numpy(), side="right")
    rank = (n - pos_right) + 1
    return pd.DataFrame(
        {"rank": rank, "percentile": np.clip(percentile, 0, 100)},
        index=scores.index,
    )


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


async def resolve_season(session: AsyncSession, code: str) -> Season:
    result = await session.execute(select(Season).where(Season.code == code))  # type: ignore[arg-type]
    season = result.scalars().first()
    if season is None:
        raise ValueError(f"Season code {code!r} not found")
    return season


async def ensure_score_definitions(
    session: AsyncSession,
) -> Dict[str, MetricDefinition]:
    """Ensure all combine-score MetricDefinitions exist, return key→def map."""
    keys = [d["metric_key"] for d in SCORE_DEFINITIONS]
    result = await session.execute(
        select(MetricDefinition).where(
            MetricDefinition.metric_key.in_(keys)  # type: ignore[attr-defined]
        )
    )
    existing = {row.metric_key: row for row in result.unique().scalars().all()}
    for defn in SCORE_DEFINITIONS:
        if defn["metric_key"] in existing:
            continue
        md = MetricDefinition(
            metric_key=defn["metric_key"],
            display_name=defn["display_name"],
            short_label=None,
            source=MetricSource.combine_score,
            statistic=MetricStatistic.raw,
            category=defn["category"],
            unit=None,
            description=f"Combine score composite for {defn['category']}",
        )
        session.add(md)
        existing[defn["metric_key"]] = md
    await session.flush()
    return existing


async def load_source_z_scores(
    session: AsyncSession,
    source: MetricSource,
    cohort: CohortType,
    season_id: Optional[int],
    position_scope_parent: Optional[str],
) -> pd.DataFrame:
    """Load z-scores from current snapshots for a given source/scope.

    Returns a DataFrame with columns: player_id, metric_key, z_score, raw_value, percentile.
    """
    # Find the current snapshot matching scope
    filters: list[Any] = [
        cast(Any, MetricSnapshot.source) == source,
        cast(Any, MetricSnapshot.cohort) == cohort,
        cast(Any, MetricSnapshot.is_current).is_(True),
    ]
    if season_id is not None:
        filters.append(cast(Any, MetricSnapshot.season_id) == season_id)
    else:
        filters.append(cast(Any, MetricSnapshot.season_id).is_(None))
    if position_scope_parent is not None:
        filters.append(
            cast(Any, MetricSnapshot.position_scope_parent) == position_scope_parent
        )
    else:
        filters.append(cast(Any, MetricSnapshot.position_scope_parent).is_(None))
    filters.append(cast(Any, MetricSnapshot.position_scope_fine).is_(None))

    result = await session.execute(
        select(MetricSnapshot.id).where(*filters).limit(1)  # type: ignore[call-overload]
    )
    snapshot_id = result.scalar_one_or_none()
    if snapshot_id is None:
        return pd.DataFrame(
            columns=["player_id", "metric_key", "z_score", "raw_value", "percentile"]
        )

    # Pull z-scores joined with metric definitions
    stmt = (
        select(  # type: ignore[call-overload]
            PlayerMetricValue.player_id,
            MetricDefinition.metric_key,
            PlayerMetricValue.z_score,
            PlayerMetricValue.raw_value,
            PlayerMetricValue.percentile,
        )
        .join(
            MetricDefinition,
            MetricDefinition.id == PlayerMetricValue.metric_definition_id,  # type: ignore[arg-type]
        )
        .where(
            PlayerMetricValue.snapshot_id == snapshot_id,  # type: ignore[arg-type]
            PlayerMetricValue.z_score.is_not(None),  # type: ignore[union-attr]
        )
    )
    result = await session.execute(stmt)
    rows = result.mappings().all()
    return pd.DataFrame(rows)


async def _next_version(session: AsyncSession, cohort: CohortType, run_key: str) -> int:
    result = await session.execute(
        select(func.max(MetricSnapshot.version)).where(
            MetricSnapshot.source == MetricSource.combine_score,  # type: ignore[arg-type]
            MetricSnapshot.run_key == run_key,  # type: ignore[arg-type]
            MetricSnapshot.cohort == cohort,  # type: ignore[arg-type]
        )
    )
    max_ver = result.scalar()
    return int(max_ver or 0) + 1


async def _delete_existing_runs(
    session: AsyncSession, cohort: CohortType, run_keys: Sequence[str]
) -> None:
    if not run_keys:
        return
    result = await session.execute(
        select(MetricSnapshot).where(
            MetricSnapshot.run_key.in_(run_keys),  # type: ignore[attr-defined]
            MetricSnapshot.cohort == cohort,  # type: ignore[arg-type]
            MetricSnapshot.source == MetricSource.combine_score,  # type: ignore[arg-type]
        )
    )
    snapshots = result.scalars().all()
    if not snapshots:
        return
    snapshot_ids = [s.id for s in snapshots if s.id is not None]
    if snapshot_ids:
        await session.execute(
            delete(PlayerMetricValue).where(
                PlayerMetricValue.snapshot_id.in_(snapshot_ids)  # type: ignore[attr-defined]
            )
        )
    for s in snapshots:
        await session.delete(s)
    await session.flush()


# ---------------------------------------------------------------------------
# Core orchestration
# ---------------------------------------------------------------------------

COMBINE_SOURCES = [
    MetricSource.combine_anthro,
    MetricSource.combine_agility,
    MetricSource.combine_shooting,
]


@dataclass
class ScopeResult:
    """Results of computing combine scores for one position scope."""

    scope_label: str
    player_count: int
    snapshot_id: Optional[int]
    run_key: str


async def compute_scores_for_scope(
    session: AsyncSession,
    *,
    cohort: CohortType,
    season_id: Optional[int],
    position_scope_parent: Optional[str],
    min_metrics: int,
    dry_run: bool,
    replace_run: bool,
    run_key: str,
    verbose: bool,
) -> ScopeResult:
    """Compute combine scores for one position scope and persist results."""
    scope_label = position_scope_parent or "all"
    if verbose:
        print(f"\n--- Combine scores: scope={scope_label} ---")

    # 1. Load z-scores from all three combine sources
    all_z: List[pd.DataFrame] = []
    for source in COMBINE_SOURCES:
        category = SOURCE_TO_CATEGORY[source]
        df = await load_source_z_scores(
            session, source, cohort, season_id, position_scope_parent
        )
        if not df.empty:
            df["category"] = category
            all_z.append(df)

    if not all_z:
        if verbose:
            print(f"  No z-score data found for scope={scope_label}")
        return ScopeResult(
            scope_label=scope_label, player_count=0, snapshot_id=None, run_key=run_key
        )

    z_df = pd.concat(all_z, ignore_index=True)
    player_ids = z_df["player_id"].unique()
    if verbose:
        print(
            f"  Loaded z-scores for {len(player_ids)} players across {len(all_z)} sources"
        )

    # 2. Compute per-player category and overall scores
    rows: List[Dict[str, Any]] = []
    for pid in player_ids:
        player_data = z_df[z_df["player_id"] == pid]

        # Build per-category z-score dicts
        category_scores: Dict[str, float] = {}
        category_details: Dict[str, Dict[str, Any]] = {}
        category_components: Dict[str, Dict[str, Dict[str, Any]]] = {}

        for cat_label, (cat_weights, _) in CATEGORY_CONFIG.items():
            cat_data = player_data[player_data["category"] == cat_label]
            if cat_data.empty:
                continue
            player_z = dict(zip(cat_data["metric_key"], cat_data["z_score"]))
            # Also capture raw values and percentiles for component detail
            player_raw = dict(zip(cat_data["metric_key"], cat_data["raw_value"]))
            player_pctl = dict(zip(cat_data["metric_key"], cat_data["percentile"]))

            score, detail = compute_category_score(player_z, cat_weights, cat_label)
            if score is not None and detail.get("metric_count", 0) >= min_metrics:
                category_scores[cat_label] = score
                category_details[cat_label] = detail
                # Enrich component details with raw_value and percentile
                for mk, comp in detail.get("components", {}).items():
                    # Resolve the source metric key for raw/percentile lookup
                    if mk == "height":
                        source_key = (
                            HEIGHT_PREFERRED
                            if HEIGHT_PREFERRED in player_raw
                            else HEIGHT_FALLBACK
                        )
                    else:
                        source_key = mk
                    if source_key in player_raw:
                        comp["raw_value"] = (
                            round(float(player_raw[source_key]), 2)
                            if pd.notna(player_raw[source_key])
                            else None
                        )
                    if source_key in player_pctl:
                        comp["percentile"] = (
                            round(float(player_pctl[source_key]), 1)
                            if pd.notna(player_pctl[source_key])
                            else None
                        )
                category_components[cat_label] = detail.get("components", {})

        # Store category scores
        for cat_label, mean_z in category_scores.items():
            metric_key = CATEGORY_SCORE_KEYS[cat_label]
            rows.append(
                {
                    "player_id": int(pid),
                    "metric_key": metric_key,
                    "raw_value": round(mean_z, 6),
                    "extra_context": category_details[cat_label],
                }
            )

        # Compute overall score if player has enough data
        if len(category_scores) >= 1:
            overall_z, overall_detail = compute_overall_score(
                category_scores, category_details
            )
            if overall_z is not None:
                rows.append(
                    {
                        "player_id": int(pid),
                        "metric_key": "combine_score_overall",
                        "raw_value": round(overall_z, 6),
                        "extra_context": overall_detail,
                    }
                )

    if not rows:
        if verbose:
            print("  No scores produced.")
        return ScopeResult(
            scope_label=scope_label, player_count=0, snapshot_id=None, run_key=run_key
        )

    scores_df = pd.DataFrame(rows)

    # 3. Compute rank/percentile per metric key (each score type ranked separately)
    definitions = await ensure_score_definitions(session)
    payload: List[PlayerMetricValue] = []

    if replace_run and not dry_run:
        await _delete_existing_runs(session, cohort, [run_key])

    # Create snapshot
    snapshot_id: Optional[int] = None
    if not dry_run:
        version = await _next_version(session, cohort, run_key)
        snapshot = MetricSnapshot(
            run_key=run_key,
            cohort=cohort,
            season_id=season_id,
            source=MetricSource.combine_score,
            population_size=len(player_ids),
            notes="Combine score composite",
            version=version,
            is_current=False,
            position_scope_parent=position_scope_parent,
            position_scope_fine=None,
        )
        session.add(snapshot)
        await session.flush()
        snapshot_id = snapshot.id

    for mk in scores_df["metric_key"].unique():
        mk_data = scores_df[scores_df["metric_key"] == mk].copy()
        raw_series = mk_data["raw_value"].astype(float)
        rp = rank_and_percentile(raw_series)

        defn = definitions[mk]
        for idx, row in mk_data.iterrows():
            rp_row = rp.loc[idx]
            pmv = PlayerMetricValue(
                snapshot_id=snapshot_id,
                metric_definition_id=defn.id,
                player_id=int(row["player_id"]),
                raw_value=float(row["raw_value"]),
                rank=int(rp_row["rank"]),
                percentile=round(float(rp_row["percentile"]), 1),
                z_score=float(row["raw_value"]),  # raw_value IS the mean z
                extra_context=row["extra_context"],
            )
            payload.append(pmv)

    player_count = int(scores_df["player_id"].nunique())
    if verbose:
        for mk in scores_df["metric_key"].unique():
            mk_count = scores_df[scores_df["metric_key"] == mk]["player_id"].nunique()
            print(f"  {mk}: {mk_count} players scored")

    if not dry_run and payload:
        session.add_all(payload)
        await session.commit()
        if verbose:
            print(
                f"  Persisted {len(payload)} PlayerMetricValue rows (snapshot_id={snapshot_id})"
            )
    elif dry_run:
        await session.rollback()
        if verbose:
            print(f"  [dry-run] Would persist {len(payload)} rows")

    return ScopeResult(
        scope_label=scope_label,
        player_count=player_count,
        snapshot_id=snapshot_id,
        run_key=run_key,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compute composite Combine Scores from pre-computed z-scores."
    )
    parser.add_argument(
        "--cohort",
        required=True,
        choices=[c.value for c in CohortType],
        help="Cohort to evaluate (e.g., current_draft, global_scope).",
    )
    parser.add_argument(
        "--season",
        help="Season code (e.g., 2024-25). Required for current_draft and global_scope.",
    )
    parser.add_argument(
        "--position-matrix",
        choices=["parent"],
        help="Compute for baseline (all positions) + parent groups (guard/wing/forward/big).",
    )
    parser.add_argument(
        "--position-scope",
        help="Compute for a single position scope (e.g., guard, pg).",
    )
    parser.add_argument(
        "--skip-baseline",
        action="store_true",
        help="Skip the all-positions baseline when using --position-matrix.",
    )
    parser.add_argument(
        "--min-metrics",
        type=int,
        default=2,
        help="Minimum individual metrics a player needs per category for a score (default: 2).",
    )
    parser.add_argument(
        "--replace-run",
        action="store_true",
        help="Delete existing combine-score snapshots with the same run key.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute without persisting.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print diagnostic output.",
    )
    return parser.parse_args(argv)


def _build_run_key(
    cohort: CohortType,
    season_code: Optional[str],
    position_scope_parent: Optional[str],
) -> str:
    if cohort == CohortType.global_scope:
        season_part = (season_code or "all").replace(" ", "_")
        base = f"combine_score_global_{season_part}"
    else:
        season_part = season_code or "all"
        base = f"combine_score|cohort={cohort.value}|season={season_part}"
    scope_part = position_scope_parent or "all"
    return f"{base}|pos={scope_part}"


def _build_scope_plan(
    args: argparse.Namespace,
) -> List[Optional[str]]:
    """Return list of position_scope_parent values to iterate over."""
    if args.position_scope:
        scope = resolve_position_scope(args.position_scope)
        if scope is None:
            return [args.position_scope]
        # Combine scores only support parent-level scopes (guard/wing/forward/big)
        # since the underlying metric snapshots use position_scope_parent.
        # Fine scopes (pg, sg, etc.) are resolved to their parent group(s).
        if scope.kind == PositionScopeKind.fine:
            return list(parents_for_scope(scope))
        return [scope.value]
    if args.position_matrix == "parent":
        scopes: List[Optional[str]] = []
        if not args.skip_baseline:
            scopes.append(None)  # all-positions baseline
        scopes.extend(PARENT_SCOPE_PRESET)
        return scopes
    return [None]  # just the baseline


async def main_async(argv: Optional[Sequence[str]] = None) -> List[ScopeResult]:
    args = parse_args(argv)
    cohort = CohortType(args.cohort)
    load_schema_modules()

    async with SessionLocal() as sa_session:
        session = cast(AsyncSession, sa_session)
        # Resolve season
        season_id: Optional[int] = None
        season_code = args.season
        if cohort == CohortType.current_draft:
            if not season_code:
                raise ValueError("--season is required for current_draft cohort")
            season = await resolve_season(session, season_code)
            season_id = season.id
        elif cohort == CohortType.global_scope:
            if not season_code:
                raise ValueError(
                    "--season is required for global_scope (use 'all' for all seasons)"
                )
            if season_code.lower() != "all":
                season = await resolve_season(session, season_code)
                season_id = season.id

        scope_plan = _build_scope_plan(args)
        results: List[ScopeResult] = []
        for position_scope in scope_plan:
            run_key = _build_run_key(cohort, season_code, position_scope)
            result = await compute_scores_for_scope(
                session,
                cohort=cohort,
                season_id=season_id,
                position_scope_parent=position_scope,
                min_metrics=args.min_metrics,
                dry_run=args.dry_run,
                replace_run=args.replace_run,
                run_key=run_key,
                verbose=args.verbose,
            )
            results.append(result)

        if not args.dry_run:
            print(
                f"\nCombine scores computed: "
                f"{sum(r.player_count for r in results)} total player-scope entries "
                f"across {len(results)} scope(s)."
            )
        else:
            print("\n[dry-run] No data persisted.")

    return results


def main(argv: Optional[Sequence[str]] = None) -> None:
    asyncio.run(main_async(argv))


if __name__ == "__main__":
    main()
