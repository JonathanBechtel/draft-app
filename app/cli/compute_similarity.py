"""Compute nearest-neighbor similarity for metric snapshots."""

from __future__ import annotations

import argparse
import asyncio
import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.fields import CohortType, MetricSource, SimilarityDimension
from app.schemas.metrics import (
    MetricDefinition,
    MetricSnapshot,
    PlayerMetricValue,
    PlayerSimilarity,
)
from app.utils.db_async import SessionLocal, load_schema_modules


SOURCE_TO_DIMENSION: Dict[MetricSource, SimilarityDimension] = {
    MetricSource.combine_anthro: SimilarityDimension.anthro,
    MetricSource.combine_agility: SimilarityDimension.combine,
    MetricSource.combine_shooting: SimilarityDimension.shooting,
}


@dataclass(frozen=True)
class SimilarityConfig:
    min_overlap: float = 0.7
    weight_anthro: float = 0.4
    weight_combine: float = 0.35
    weight_shooting: float = 0.25
    max_neighbors: Optional[int] = None  # None = keep all


async def resolve_snapshot(
    session: AsyncSession,
    snapshot_id: Optional[int],
    run_key: Optional[str],
    source: Optional[str],
    cohort: Optional[str] = None,
) -> MetricSnapshot:
    if snapshot_id is not None:
        sid = int(snapshot_id)
        stmt = select(MetricSnapshot).where(MetricSnapshot.id == sid)  # type: ignore[arg-type]
        result = await session.execute(stmt)
        snap = result.scalar_one_or_none()
        if not snap:
            raise ValueError(f"Snapshot {snapshot_id} not found")
        return snap

    if run_key and source:
        src = MetricSource(source)
        stmt = select(MetricSnapshot).where(MetricSnapshot.run_key == run_key)  # type: ignore[arg-type]
        stmt = stmt.where(MetricSnapshot.source == src)  # type: ignore[arg-type]
        if cohort:
            stmt = stmt.where(MetricSnapshot.cohort == CohortType(cohort))  # type: ignore[arg-type]
        stmt = stmt.order_by(MetricSnapshot.version.desc())  # type: ignore[attr-defined]
        result = await session.execute(stmt)
        snap = result.scalars().first()
        if not snap:
            raise ValueError(
                f"No snapshot found for run_key={run_key!r} source={source!r}"
            )
        return snap

    raise ValueError("Provide --snapshot-id or (--run-key and --source)")


async def fetch_metric_rows(session: AsyncSession, snapshot_id: int) -> pd.DataFrame:
    select_clause: Any = select
    stmt = (
        select_clause(
            PlayerMetricValue.player_id,
            MetricDefinition.metric_key,
            MetricDefinition.source,
            PlayerMetricValue.z_score,
        )
        .join(
            MetricDefinition,
            MetricDefinition.id == PlayerMetricValue.metric_definition_id,
        )
        .where(
            PlayerMetricValue.snapshot_id == snapshot_id,  # type: ignore[arg-type]
            PlayerMetricValue.z_score.is_not(None),  # type: ignore[union-attr]
        )
    )
    result = await session.execute(stmt)
    rows = result.all()
    if not rows:
        return pd.DataFrame(columns=["player_id", "metric_key", "source", "z_score"])
    return pd.DataFrame(rows, columns=["player_id", "metric_key", "source", "z_score"])


def build_feature_frames(df: pd.DataFrame) -> Dict[SimilarityDimension, pd.DataFrame]:
    frames: Dict[SimilarityDimension, pd.DataFrame] = {}
    if df.empty:
        return frames

    df["dimension"] = df["source"].apply(
        lambda s: SOURCE_TO_DIMENSION.get(MetricSource(s))
    )  # type: ignore[arg-type]
    for dimension in SimilarityDimension:
        subset = df[df["dimension"] == dimension]
        if subset.empty:
            continue
        pivoted = subset.pivot(
            index="player_id", columns="metric_key", values="z_score"
        )
        frames[dimension] = pivoted
    return frames


def _alpha_for_distances(distances: List[float]) -> float:
    clean = [d for d in distances if d is not None and d >= 0]
    if not clean:
        return 0.0
    median_d = float(np.median(clean))
    if median_d <= 1e-6:
        return 1.0
    alpha = math.log(2) / median_d
    # Bound alpha to avoid exploding similarities when clusters are tight
    return max(1e-3, min(alpha, 10.0))


def _standardized_euclidean(
    values: np.ndarray,
    mask: np.ndarray,
    players: np.ndarray,
    total_metrics: int,
    min_overlap: float,
) -> Tuple[Dict[Tuple[int, int], float], Dict[Tuple[int, int], float]]:
    distances: Dict[Tuple[int, int], float] = {}
    overlaps: Dict[Tuple[int, int], float] = {}
    # Variance per column with nan handling
    variances = np.nanvar(values, axis=0)
    variances[variances <= 1e-6] = (
        1.0  # avoid divide-by-zero; fallback to unit variance
    )

    for i in range(len(players)):
        for j in range(len(players)):
            if i == j:
                continue
            overlap_mask = mask[i] & mask[j]
            overlap_count = int(overlap_mask.sum())
            if overlap_count == 0 or total_metrics == 0:
                continue
            overlap_pct = overlap_count / total_metrics
            if overlap_pct < min_overlap:
                continue
            diff = values[i, overlap_mask] - values[j, overlap_mask]
            var_slice = variances[overlap_mask]
            dist = float(np.sqrt(np.mean((diff**2) / var_slice)))
            distances[(int(players[i]), int(players[j]))] = dist
            overlaps[(int(players[i]), int(players[j]))] = overlap_pct
    return distances, overlaps


def compute_dimension_similarity(
    dimension: SimilarityDimension,
    frame: pd.DataFrame,
    min_overlap: float,
) -> Tuple[
    Dict[Tuple[int, int], float],
    Dict[Tuple[int, int], float],
    Dict[Tuple[int, int], float],
]:
    """Return distance_map, similarity_map, overlap_map keyed by (anchor, neighbor).

    Anthro/Combine: standardized Euclidean (variance-weighted).
    Shooting: keep the same distance logic for now.
    Pairs below overlap threshold are skipped.
    """
    distance_map: Dict[Tuple[int, int], float] = {}
    overlap_map: Dict[Tuple[int, int], float] = {}

    if frame.empty:
        return distance_map, {}, overlap_map

    players = frame.index.to_numpy()
    values = frame.to_numpy(dtype=float)
    mask = ~np.isnan(values)
    total_metrics = frame.shape[1]

    if dimension in (
        SimilarityDimension.anthro,
        SimilarityDimension.combine,
        SimilarityDimension.composite,
    ):
        dist_map, overlap_map = _standardized_euclidean(
            values, mask, players, total_metrics, min_overlap
        )
        distance_map = dist_map
    else:
        # Shooting or any other dimension: keep RMS on shared metrics
        pair_distances: List[float] = []
        for i in range(len(players)):
            for j in range(len(players)):
                if i == j:
                    continue
                overlap_mask = mask[i] & mask[j]
                overlap_count = int(overlap_mask.sum())
                if overlap_count == 0 or total_metrics == 0:
                    continue
                overlap_pct = overlap_count / total_metrics
                if overlap_pct < min_overlap:
                    continue
                diff = values[i, overlap_mask] - values[j, overlap_mask]
                dist = float(np.sqrt(np.mean(diff**2)))
                distance_map[(int(players[i]), int(players[j]))] = dist
                overlap_map[(int(players[i]), int(players[j]))] = overlap_pct
                pair_distances.append(dist)

    alpha = _alpha_for_distances(list(distance_map.values()))
    similarity_map: Dict[Tuple[int, int], float] = {}
    for key, dist in distance_map.items():
        sim = 100 * math.exp(-alpha * dist) if alpha > 0 else 0.0
        similarity_map[key] = max(0.0, min(100.0, sim))

    return distance_map, similarity_map, overlap_map


def compute_composite_similarity(
    distance_by_dim: Dict[SimilarityDimension, Dict[Tuple[int, int], float]],
    weights: Dict[SimilarityDimension, float],
) -> Dict[Tuple[int, int], float]:
    composite_dist: Dict[Tuple[int, int], float] = {}
    pairs: set[Tuple[int, int]] = set()
    for dist_map in distance_by_dim.values():
        pairs.update(dist_map.keys())

    for pair in pairs:
        weighted_sum = 0.0
        weight_total = 0.0
        for dim, dist_map in distance_by_dim.items():
            w = weights.get(dim, 0.0)
            if w <= 0:
                continue
            d = dist_map.get(pair)
            if d is None:
                continue
            weighted_sum += w * d
            weight_total += w
        if weight_total == 0:
            continue
        composite_dist[pair] = weighted_sum / weight_total

    alpha = _alpha_for_distances(list(composite_dist.values()))
    if alpha == 0:
        return {}

    similarities: Dict[Tuple[int, int], float] = {}
    for pair, dist in composite_dist.items():
        sim = 100 * math.exp(-alpha * dist)
        similarities[pair] = max(0.0, min(100.0, sim))
    return similarities


def rank_neighbors(sim_map: Dict[Tuple[int, int], float]) -> Dict[Tuple[int, int], int]:
    """Return rank (1-based) per (anchor, neighbor) within anchor."""
    grouped: Dict[int, List[Tuple[int, float]]] = defaultdict(list)
    for (anchor, neighbor), sim in sim_map.items():
        grouped[anchor].append((neighbor, sim))

    ranks: Dict[Tuple[int, int], int] = {}
    for anchor, items in grouped.items():
        items_sorted = sorted(items, key=lambda x: x[1], reverse=True)
        for idx, (neighbor, _) in enumerate(items_sorted, start=1):
            ranks[(anchor, neighbor)] = idx
    return ranks


async def write_similarity(
    session: AsyncSession,
    snapshot_id: int,
    frames: Dict[SimilarityDimension, pd.DataFrame],
    config: SimilarityConfig,
) -> None:
    # Compute per-dimension distances/similarities
    distance_by_dim: Dict[SimilarityDimension, Dict[Tuple[int, int], float]] = {}
    similarity_by_dim: Dict[SimilarityDimension, Dict[Tuple[int, int], float]] = {}
    overlap_by_dim: Dict[SimilarityDimension, Dict[Tuple[int, int], float]] = {}

    for dim, frame in frames.items():
        dist_map, sim_map, overlap_map = compute_dimension_similarity(
            dim, frame, config.min_overlap
        )
        distance_by_dim[dim] = dist_map
        similarity_by_dim[dim] = sim_map
        overlap_by_dim[dim] = overlap_map

    composite_weights = {
        SimilarityDimension.anthro: config.weight_anthro,
        SimilarityDimension.combine: config.weight_combine,
        SimilarityDimension.shooting: config.weight_shooting,
    }
    composite_sim = compute_composite_similarity(distance_by_dim, composite_weights)

    # Precompute ranks
    ranks_by_dim = {
        dim: rank_neighbors(sim_map) for dim, sim_map in similarity_by_dim.items()
    }
    composite_ranks = rank_neighbors(composite_sim)

    # Clear existing rows for this snapshot
    await session.execute(
        delete(PlayerSimilarity).where(
            PlayerSimilarity.snapshot_id == snapshot_id  # type: ignore[arg-type]
        )
    )

    payload: List[Dict[str, Any]] = []

    # Dimensions
    for dim, sim_map in similarity_by_dim.items():
        ranks = ranks_by_dim.get(dim, {})
        for (anchor, neighbor), sim in sim_map.items():
            rank_val = ranks.get((anchor, neighbor))
            if (
                config.max_neighbors is not None
                and rank_val
                and rank_val > config.max_neighbors
            ):
                continue
            payload.append(
                {
                    "snapshot_id": snapshot_id,
                    "dimension": dim,
                    "anchor_player_id": anchor,
                    "comparison_player_id": neighbor,
                    "similarity_score": sim,
                    "distance": distance_by_dim.get(dim, {}).get((anchor, neighbor)),
                    "overlap_pct": overlap_by_dim.get(dim, {}).get((anchor, neighbor)),
                    "rank_within_anchor": rank_val,
                }
            )

    # Composite
    for (anchor, neighbor), sim in composite_sim.items():
        rank_val = composite_ranks.get((anchor, neighbor))
        if (
            config.max_neighbors is not None
            and rank_val
            and rank_val > config.max_neighbors
        ):
            continue
        payload.append(
            {
                "snapshot_id": snapshot_id,
                "dimension": SimilarityDimension.composite,
                "anchor_player_id": anchor,
                "comparison_player_id": neighbor,
                "similarity_score": sim,
                "distance": None,
                "overlap_pct": None,
                "rank_within_anchor": rank_val,
            }
        )

    # Batch raw inserts to avoid ORM overhead (no RETURNING, no identity map)
    # and keep each statement small enough to avoid connection timeouts.
    batch_size = 2000
    for i in range(0, len(payload), batch_size):
        await session.execute(insert(PlayerSimilarity), payload[i : i + batch_size])
    await session.commit()
    print(f"[similarity] snapshot={snapshot_id} wrote {len(payload)} rows")


async def compute_for_snapshot(
    session: AsyncSession, snapshot_id: int, config: SimilarityConfig
) -> None:
    metric_rows = await fetch_metric_rows(session, snapshot_id)
    # End the read transaction so the connection doesn't sit idle during the
    # O(n²) distance computation (can take minutes for large cohorts).
    await session.rollback()
    frames = build_feature_frames(metric_rows)
    if not frames:
        print(f"No metrics found for snapshot {snapshot_id}; nothing to compute.")
        return
    await write_similarity(session, snapshot_id, frames, config)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compute player similarity for a metric snapshot."
    )
    parser.add_argument("--snapshot-id", type=int, help="Metric snapshot ID to target")
    parser.add_argument(
        "--run-key", help="Run key to resolve the snapshot (uses latest version)"
    )
    parser.add_argument(
        "--source",
        help="Source key required when using --run-key (e.g., combine_anthro/combine_agility/combine_shooting)",
    )
    parser.add_argument(
        "--cohort",
        choices=[c.value for c in CohortType],
        help="Optional cohort filter when resolving snapshots (e.g., global_scope)",
    )
    parser.add_argument(
        "--min-overlap",
        type=float,
        default=0.7,
        help="Minimum fraction of shared metrics required to score a pair (0-1)",
    )
    parser.add_argument(
        "--max-neighbors",
        type=int,
        default=None,
        help="Optional cap on neighbors stored per anchor/dimension",
    )
    parser.add_argument(
        "--weights",
        nargs=3,
        metavar=("ANTHRO", "COMBINE", "SHOOTING"),
        type=float,
        default=[0.4, 0.35, 0.25],
        help="Composite weights for anthro/combine/shooting distances (must sum to ~1)",
    )
    return parser.parse_args(argv)


async def main_async(argv: Optional[Sequence[str]] = None) -> None:
    args = parse_args(argv)
    load_schema_modules()

    weights = args.weights
    weight_sum = sum(weights)
    if weight_sum <= 0:
        raise ValueError("Composite weights must sum to a positive value")
    norm_weights = [w / weight_sum for w in weights]

    config = SimilarityConfig(
        min_overlap=args.min_overlap,
        weight_anthro=norm_weights[0],
        weight_combine=norm_weights[1],
        weight_shooting=norm_weights[2],
        max_neighbors=args.max_neighbors,
    )

    async with SessionLocal() as session:
        snapshot = await resolve_snapshot(
            session, args.snapshot_id, args.run_key, args.source
        )
        assert snapshot.id is not None
        await compute_for_snapshot(session, snapshot.id, config)


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
