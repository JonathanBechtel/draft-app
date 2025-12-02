from __future__ import annotations

import argparse
import asyncio
from typing import Any, Optional, Sequence, Set

from sqlalchemy import select

from app.models.fields import CohortType, MetricSource
from app.schemas.metrics import MetricSnapshot
from app.scripts.compute_similarity import (
    SimilarityConfig,
    compute_for_snapshot,
)
from app.utils.db_async import SessionLocal, load_schema_modules


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compute similarity for metric snapshots (default: all current snapshots)."
    )
    parser.add_argument(
        "--snapshot-ids",
        nargs="+",
        type=int,
        help="Explicit snapshot IDs to compute (otherwise uses all is_current snapshots)",
    )
    parser.add_argument(
        "--sources",
        nargs="+",
        choices=[s.value for s in MetricSource],
        help="Limit to these sources when auto-selecting current snapshots",
    )
    parser.add_argument(
        "--cohort",
        choices=[c.value for c in CohortType],
        help="Optional cohort filter when auto-selecting snapshots (e.g., global_scope)",
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
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Persist results (omit to run in dry-run mode for selected snapshots)",
    )
    return parser.parse_args(argv)


async def run(argv: Optional[Sequence[str]] = None) -> None:
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
        target_snapshots = []
        if args.snapshot_ids:
            stmt = select(MetricSnapshot).where(
                MetricSnapshot.id.in_(args.snapshot_ids)  # type: ignore[attr-defined,union-attr]
            )
            result = await session.execute(stmt)
            target_snapshots = list(result.scalars())
        else:
            is_current_clause: Any = MetricSnapshot.is_current
            stmt = select(MetricSnapshot).where(is_current_clause.is_(True))
            if args.sources:
                selected_sources: Set[MetricSource] = {
                    MetricSource(s) for s in args.sources
                }
                stmt = stmt.where(
                    MetricSnapshot.source.in_(selected_sources)  # type: ignore[attr-defined,arg-type]
                )
            if args.cohort:
                stmt = stmt.where(
                    MetricSnapshot.cohort == CohortType(args.cohort)  # type: ignore[arg-type]
                )
            result = await session.execute(stmt)
            target_snapshots = list(result.scalars())

        if not target_snapshots:
            print("No snapshots selected for similarity computation.")
            return

        for snap in target_snapshots:
            label = f"id={snap.id} run_key={snap.run_key} src={snap.source.value}"
            if not args.execute:
                print(f"[dry-run] Would compute similarity for snapshot {label}")
                continue
            print(f"[similarity] Computing for snapshot {label}")
            await compute_for_snapshot(session, snap, config)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
