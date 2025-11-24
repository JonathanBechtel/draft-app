from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.fields import CohortType, MetricCategory, MetricSource
from app.models.position_taxonomy import (
    FINE_SCOPE_PRESET,
    PARENT_SCOPE_PRESET,
)
from app.schemas.combine_agility import CombineAgility
from app.schemas.combine_anthro import CombineAnthro
from app.schemas.combine_shooting import CombineShooting
from app.schemas.metrics import MetricSnapshot
from app.schemas.seasons import Season
from app.scripts.compute_metrics import main_async as compute_metrics_main
from app.utils.db_async import SessionLocal, load_schema_modules


@dataclass(frozen=True)
class Anchor:
    cohort: CohortType
    season_id: Optional[int]
    pos_parent: Optional[str]
    pos_fine: Optional[str]


def _categories_for_sources(sources: Set[MetricSource]) -> List[str]:
    cats: Set[MetricCategory] = set()
    if MetricSource.combine_anthro in sources:
        cats.add(MetricCategory.anthropometrics)
    perf_needed = any(
        s in sources
        for s in (MetricSource.combine_agility, MetricSource.combine_shooting)
    )
    if perf_needed:
        cats.add(MetricCategory.combine_performance)
    return [c.value for c in sorted(cats, key=lambda c: c.value)]


async def seasons_with_combine_data(session: AsyncSession) -> Dict[int, str]:
    ids: Set[int] = set()
    for model in (CombineAnthro, CombineAgility, CombineShooting):
        stmt = select(model.season_id).distinct()  # type: ignore[call-overload]
        result = await session.execute(stmt)
        ids.update(x for (x,) in result.all() if x is not None)
    if not ids:
        return {}
    stmt_season = select(Season.id, Season.code).where(cast(Any, Season.id).in_(ids))  # type: ignore[call-overload]
    result = await session.execute(stmt_season)
    return {row[0]: row[1] for row in result.all()}


async def existing_snapshot_keys(
    session: AsyncSession, cohorts: Set[CohortType], sources: Set[MetricSource]
) -> Set[Tuple[CohortType, Optional[int], MetricSource, Optional[str], Optional[str]]]:
    stmt = select(  # type: ignore[call-overload]
        MetricSnapshot.cohort,
        MetricSnapshot.season_id,
        MetricSnapshot.source,
        MetricSnapshot.position_scope_parent,
        MetricSnapshot.position_scope_fine,
    ).where(
        cast(Any, MetricSnapshot.cohort).in_(list(cohorts)),
        cast(Any, MetricSnapshot.source).in_(list(sources)),
    )
    result = await session.execute(stmt)
    keys: Set[
        Tuple[CohortType, Optional[int], MetricSource, Optional[str], Optional[str]]
    ] = set()
    for cohort, season_id, source, pos_parent, pos_fine in result.all():
        keys.add((cohort, season_id, source, pos_parent, pos_fine))
    return keys


def build_anchor_space(
    *,
    cohorts: Set[CohortType],
    season_ids: Iterable[int],
    include_baseline: bool,
    include_parent: bool,
    include_fine: bool,
) -> List[Anchor]:
    anchors: List[Anchor] = []
    for cohort in sorted(cohorts, key=lambda c: c.value):
        # Season dimension only for current_draft
        season_candidates: List[Optional[int]]
        if cohort == CohortType.current_draft:
            season_candidates = list(sorted(season_ids))
        else:
            season_candidates = [None]

        for sid in season_candidates:
            if include_baseline:
                anchors.append(
                    Anchor(cohort=cohort, season_id=sid, pos_parent=None, pos_fine=None)
                )
            if include_parent:
                for parent in PARENT_SCOPE_PRESET:
                    anchors.append(
                        Anchor(
                            cohort=cohort,
                            season_id=sid,
                            pos_parent=parent,
                            pos_fine=None,
                        )
                    )
            if include_fine:
                for fine in FINE_SCOPE_PRESET:
                    anchors.append(
                        Anchor(
                            cohort=cohort, season_id=sid, pos_parent=None, pos_fine=fine
                        )
                    )
    return anchors


async def run_backfill(argv: Optional[Sequence[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description="Backfill missing metric snapshot runs."
    )
    parser.add_argument(
        "--cohorts",
        nargs="+",
        choices=[c.value for c in CohortType],
        default=[c.value for c in CohortType],
        help="Cohorts to include (default: all)",
    )
    parser.add_argument(
        "--sources",
        nargs="+",
        choices=[s.value for s in MetricSource],
        default=[
            MetricSource.combine_anthro.value,
            MetricSource.combine_agility.value,
            MetricSource.combine_shooting.value,
        ],
        help="Metric sources to backfill (default: all combine sources)",
    )
    parser.add_argument(
        "--include-fine",
        action="store_true",
        help="Also backfill fine/hybrid position scopes (pg, sg, ..., pf-c)",
    )
    parser.add_argument(
        "--no-baseline",
        action="store_true",
        help="Skip baseline (all positions) runs",
    )
    parser.add_argument(
        "--no-parent",
        action="store_true",
        help="Skip parent position sweeps (guard/wing/forward/big)",
    )
    parser.add_argument(
        "--season-codes",
        nargs="+",
        help="Limit current_draft backfill to these season codes (e.g., 2024-25 2023-24)",
    )
    parser.add_argument(
        "--min-sample",
        type=int,
        default=None,
        help="Override minimum sample size for metric computation",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Persist results (omit to run in dry-run mode)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print detailed plan and progress",
    )
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="Only print the backfill plan; do not invoke computations",
    )
    args = parser.parse_args(argv)

    load_schema_modules()
    async with SessionLocal() as session:
        selected_cohorts: Set[CohortType] = {CohortType(c) for c in args.cohorts}
        selected_sources: Set[MetricSource] = {MetricSource(s) for s in args.sources}

        # Determine season scope for current_draft
        season_id_to_code = await seasons_with_combine_data(session)
        if args.season_codes:
            codes = set(args.season_codes)
            season_id_to_code = {
                sid: code for sid, code in season_id_to_code.items() if code in codes
            }

        # What exists already
        existing = await existing_snapshot_keys(
            session, selected_cohorts, selected_sources
        )

        # Build desired anchors
        anchors = build_anchor_space(
            cohorts=selected_cohorts,
            season_ids=season_id_to_code.keys(),
            include_baseline=not args.no_baseline,
            include_parent=not args.no_parent,
            include_fine=args.include_fine,
        )

        # Find missing sources per anchor
        plan: List[Tuple[Anchor, Set[MetricSource]]] = []
        for anchor in anchors:
            missing: Set[MetricSource] = set()
            for src in selected_sources:
                key = (
                    anchor.cohort,
                    anchor.season_id,
                    src,
                    anchor.pos_parent,
                    anchor.pos_fine,
                )
                if key not in existing:
                    missing.add(src)
            if missing:
                plan.append((anchor, missing))

        if args.verbose:
            print(f"Planned backfill actions: {len(plan)} anchors with missing sources")
            for anchor, missing in plan:
                where = []
                where.append(f"cohort={anchor.cohort.value}")
                if anchor.season_id is not None:
                    where.append(
                        f"season={season_id_to_code.get(anchor.season_id, str(anchor.season_id))}"
                    )
                if anchor.pos_parent:
                    where.append(f"parent={anchor.pos_parent}")
                if anchor.pos_fine:
                    where.append(f"fine={anchor.pos_fine}")
                print(
                    " - "
                    + ", ".join(where)
                    + f" | sources={[s.value for s in sorted(missing, key=lambda s: s.value)]}"
                )

        # Execute plan by grouping per anchor and calling compute_metrics with --sources
        executed = 0
        ts = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        if not args.plan_only:
            for anchor, missing_sources in plan:
                argv_inner: List[str] = ["--cohort", anchor.cohort.value]
                if anchor.cohort == CohortType.current_draft:
                    if anchor.season_id is None:
                        # Should not occur; skip defensively
                        continue
                    season_code = season_id_to_code.get(anchor.season_id)
                    if not season_code:
                        continue
                    argv_inner.extend(["--season", season_code])
                else:
                    season_code = None
                # Position scope
                if anchor.pos_parent:
                    argv_inner.extend(["--position-scope", anchor.pos_parent])
                elif anchor.pos_fine:
                    argv_inner.extend(["--position-scope", anchor.pos_fine])

                # Categories + sources
                cats = _categories_for_sources(missing_sources)
                if cats:
                    argv_inner.extend(["--categories", *cats])
                argv_inner.extend(
                    [
                        "--sources",
                        *[
                            s.value
                            for s in sorted(missing_sources, key=lambda s: s.value)
                        ],
                    ]
                )

                # Min sample (optional)
                if args.min_sample is not None:
                    argv_inner.extend(["--min-sample", str(args.min_sample)])

                # Stable, unique run key per anchor (avoid collisions when running many anchors quickly)
                # Compose suffix from scope label
                scope_label = anchor.pos_parent or anchor.pos_fine
                base_season = season_code or "all"
                if scope_label:
                    run_key = f"metrics_{base_season}_{ts}__{scope_label}"
                else:
                    run_key = f"metrics_{base_season}_{ts}"
                argv_inner.extend(["--run-key", run_key])

                # Execution mode
                if not args.execute:
                    argv_inner.append("--dry-run")

                if args.verbose:
                    print(
                        "Executing:",
                        "python -m app.scripts.compute_metrics",
                        " ".join(argv_inner),
                    )
                await compute_metrics_main(argv_inner)
                executed += 1

            if args.verbose:
                print(f"Completed {executed} backfill invocations.")


def main(argv: Optional[Sequence[str]] = None) -> None:
    asyncio.run(run_backfill(argv))


if __name__ == "__main__":
    main()
