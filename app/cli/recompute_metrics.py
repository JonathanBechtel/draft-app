from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence, Set, Tuple, cast

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.cli.compute_metrics import main_async as compute_metrics_main
from app.models.fields import CohortType, MetricSource
from app.models.position_taxonomy import PARENT_SCOPE_PRESET
from app.schemas.combine_agility import CombineAgility
from app.schemas.combine_anthro import CombineAnthro
from app.schemas.combine_shooting import CombineShooting
from app.schemas.metrics import MetricSnapshot
from app.schemas.seasons import Season
from app.utils.db_async import SessionLocal, load_schema_modules


GLOBAL_COHORTS_DEFAULT: Tuple[CohortType, ...] = (
    CohortType.global_scope,
    CohortType.all_time_draft,
    CohortType.current_nba,
    CohortType.all_time_nba,
)


@dataclass(frozen=True)
class SnapshotContext:
    cohort: CohortType
    source: MetricSource
    season_id: Optional[int]
    position_scope_parent: Optional[str]
    position_scope_fine: Optional[str]


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Recompute metric snapshots across draft seasons and global cohorts.\n\n"
            "This is intended as a one-shot command to refresh percentiles/ranks in bulk.\n"
            "It delegates to app.cli.compute_metrics and can optionally promote the new\n"
            "snapshots to is_current for the app to select."
        )
    )
    parser.add_argument(
        "--draft-seasons",
        nargs="*",
        help=(
            "Limit current_draft recompute to these season codes (e.g., 2025-26 2024-25). "
            "Omit to recompute for every season in the seasons table."
        ),
    )
    parser.add_argument(
        "--skip-current-draft",
        action="store_true",
        help="Skip current_draft season runs entirely.",
    )
    parser.add_argument(
        "--global-cohorts",
        nargs="+",
        choices=[c.value for c in CohortType],
        default=[c.value for c in GLOBAL_COHORTS_DEFAULT],
        help=(
            "Which non-season cohorts to recompute (default: global_scope, all_time_draft, "
            "current_nba, all_time_nba)."
        ),
    )
    parser.add_argument(
        "--sources",
        nargs="+",
        choices=[
            MetricSource.combine_anthro.value,
            MetricSource.combine_agility.value,
            MetricSource.combine_shooting.value,
        ],
        default=[
            MetricSource.combine_anthro.value,
            MetricSource.combine_agility.value,
            MetricSource.combine_shooting.value,
        ],
        help="Metric sources to compute (default: all combine sources).",
    )
    parser.add_argument(
        "--min-sample",
        type=int,
        default=3,
        help="Minimum sample size required to emit a metric (default: 3).",
    )
    parser.add_argument(
        "--replace-run",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "When enabled, delete existing snapshots for the same run_key before inserting. "
            "Defaults to disabled so you can verify results before deleting old snapshots."
        ),
    )
    parser.add_argument(
        "--skip-baseline",
        action="store_true",
        help="Skip the all-positions baseline when using the parent matrix.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Persist results (omit to run in dry-run mode).",
    )
    parser.add_argument(
        "--promote",
        action="store_true",
        help=(
            "After computation, mark newly computed snapshots is_current=true for their "
            "context (cohort/source/season/scope), demoting others."
        ),
    )
    parser.add_argument(
        "--similarity",
        action="store_true",
        help=(
            "After promotion, recompute player similarity for all promoted snapshots. "
            "Requires --promote and --execute."
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print each underlying compute_metrics invocation and promotion.",
    )
    return parser.parse_args(argv)


async def _resolve_season_ids(
    db: AsyncSession, season_codes: Optional[Sequence[str]]
) -> List[Tuple[int, str]]:
    stmt = select(Season.id, Season.code)  # type: ignore[call-overload]
    stmt = stmt.order_by(Season.start_year)  # type: ignore[arg-type]
    if season_codes:
        stmt = stmt.where(cast(Any, Season.code).in_(list(season_codes)))  # type: ignore[call-overload]
    else:
        ids: Set[int] = set()
        for model in (CombineAnthro, CombineAgility, CombineShooting):
            result = await db.execute(
                select(model.season_id).distinct()  # type: ignore[call-overload]
            )
            ids.update(x for (x,) in result.all() if x is not None)
        if not ids:
            return []
        stmt = stmt.where(cast(Any, Season.id).in_(ids))  # type: ignore[call-overload]
    result = await db.execute(stmt)
    seasons: List[Tuple[int, str]] = []
    for sid, code in result.all():
        if sid is None or code is None:
            continue
        seasons.append((int(sid), str(code)))
    return seasons


def _base_run_key_for_invocation(
    *, cohort: CohortType, season_code: Optional[str]
) -> str:
    if cohort == CohortType.global_scope:
        season_part = (season_code or "all").replace(" ", "_")
        return f"metrics_global_{season_part}"
    season_part = season_code or "all"
    return f"cohort={cohort.value}|season={season_part}"


def _scope_run_keys(
    *,
    base_run_key: str,
    min_sample: int,
    include_baseline: bool,
) -> List[Tuple[Optional[str], str]]:
    keys: List[Tuple[Optional[str], str]] = []
    if include_baseline:
        keys.append((None, f"{base_run_key}|pos=all|min={min_sample}"))
    for parent in PARENT_SCOPE_PRESET:
        keys.append((parent, f"{base_run_key}|pos={parent}|min={min_sample}"))
    return keys


def _run_key_for_source(
    *, cohort: CohortType, source: MetricSource, scope_run_key: str
) -> str:
    if cohort == CohortType.global_scope:
        return f"{scope_run_key}_{source.value}"
    return scope_run_key


def _context_filters(context: SnapshotContext) -> List[Any]:
    """Build SQLAlchemy filter clauses for a SnapshotContext."""
    filters: List[Any] = [
        cast(Any, MetricSnapshot.cohort) == context.cohort,
        cast(Any, MetricSnapshot.source) == context.source,
    ]
    if context.season_id is None:
        filters.append(cast(Any, MetricSnapshot.season_id).is_(None))
    else:
        filters.append(cast(Any, MetricSnapshot.season_id) == context.season_id)
    if context.position_scope_parent is None:
        filters.append(cast(Any, MetricSnapshot.position_scope_parent).is_(None))
    else:
        filters.append(
            cast(Any, MetricSnapshot.position_scope_parent)
            == context.position_scope_parent
        )
    if context.position_scope_fine is None:
        filters.append(cast(Any, MetricSnapshot.position_scope_fine).is_(None))
    else:
        filters.append(
            cast(Any, MetricSnapshot.position_scope_fine) == context.position_scope_fine
        )
    return filters


async def _find_current_snapshot_id(
    db: AsyncSession, context: SnapshotContext
) -> Optional[int]:
    """Find the id of the is_current snapshot for a given context."""
    filters = _context_filters(context)
    filters.append(cast(Any, MetricSnapshot.is_current).is_(True))
    result = await db.execute(
        select(MetricSnapshot.id).where(*filters).limit(1)  # type: ignore[call-overload]
    )
    return result.scalar_one_or_none()


async def _promote_snapshot(
    db: AsyncSession, context: SnapshotContext, *, run_key: str, verbose: bool
) -> None:
    filters = _context_filters(context)

    result = await db.execute(
        select(MetricSnapshot.id)  # type: ignore[call-overload]
        .where(*filters)
        .where(cast(Any, MetricSnapshot.run_key) == run_key)
        .order_by(MetricSnapshot.version.desc())  # type: ignore[attr-defined]
        .limit(1)
    )
    target_id = result.scalar_one_or_none()
    if target_id is None:
        if verbose:
            print(f"[promote] No snapshot found for run_key={run_key}")
        return

    if verbose:
        print(
            "[promote] cohort="
            + context.cohort.value
            + f" source={context.source.value} season_id={context.season_id} "
            + f"parent={context.position_scope_parent} -> snapshot_id={target_id}"
        )

    # Avoid transient partial-unique-index violations (uq_metric_snapshots_current) by
    # demoting the existing current snapshot first, then promoting the target.
    await db.execute(
        update(MetricSnapshot)
        .where(*filters)
        .where(cast(Any, MetricSnapshot.id) != int(target_id))
        .values(is_current=False)
    )
    await db.execute(
        update(MetricSnapshot)
        .where(cast(Any, MetricSnapshot.id) == int(target_id))
        .values(is_current=True)
    )


async def run_recompute(argv: Optional[Sequence[str]] = None) -> None:
    args = parse_args(argv)
    load_schema_modules()
    min_sample = max(1, args.min_sample)

    selected_sources: List[MetricSource] = [MetricSource(s) for s in args.sources]
    global_cohorts: List[CohortType] = [CohortType(c) for c in args.global_cohorts]

    include_baseline = not args.skip_baseline

    async with SessionLocal() as db:
        seasons = []
        if not args.skip_current_draft:
            seasons = await _resolve_season_ids(db, args.draft_seasons)
            if args.draft_seasons and not seasons:
                raise ValueError(
                    "No seasons matched the provided --draft-seasons codes."
                )

        promotion_jobs: List[Tuple[SnapshotContext, str]] = []

        async def _run_compute_for(
            *,
            cohort: CohortType,
            season_code: Optional[str],
            season_id: Optional[int],
        ) -> None:
            argv_inner: List[str] = [
                "--cohort",
                cohort.value,
                "--position-matrix",
                "parent",
                "--sources",
                *[s.value for s in selected_sources],
                "--min-sample",
                str(min_sample),
            ]
            if args.replace_run:
                argv_inner.append("--replace-run")
            if cohort == CohortType.current_draft:
                if not season_code:
                    raise ValueError("current_draft requires a season code.")
                argv_inner.extend(["--season", season_code])
            elif cohort == CohortType.global_scope:
                argv_inner.extend(["--season", season_code or "all"])

            if args.skip_baseline:
                argv_inner.append("--matrix-skip-baseline")

            if not args.execute:
                argv_inner.append("--dry-run")

            if args.verbose:
                print(
                    "Executing: python -m app.cli.compute_metrics "
                    + " ".join(argv_inner)
                )

            await compute_metrics_main(argv_inner)

            base_run_key = _base_run_key_for_invocation(
                cohort=cohort, season_code=season_code
            )
            for parent_scope, scope_run_key in _scope_run_keys(
                base_run_key=base_run_key,
                min_sample=min_sample,
                include_baseline=include_baseline,
            ):
                for source in selected_sources:
                    run_key = _run_key_for_source(
                        cohort=cohort, source=source, scope_run_key=scope_run_key
                    )
                    promotion_jobs.append(
                        (
                            SnapshotContext(
                                cohort=cohort,
                                source=source,
                                season_id=season_id
                                if cohort == CohortType.current_draft
                                else None,
                                position_scope_parent=parent_scope,
                                position_scope_fine=None,
                            ),
                            run_key,
                        )
                    )

        for cohort in global_cohorts:
            season_code = "all" if cohort == CohortType.global_scope else None
            await _run_compute_for(
                cohort=cohort, season_code=season_code, season_id=None
            )

        for season_id, season_code in seasons:
            await _run_compute_for(
                cohort=CohortType.current_draft,
                season_code=season_code,
                season_id=season_id,
            )

        if args.promote and args.execute:
            for context, run_key in promotion_jobs:
                await _promote_snapshot(
                    db, context, run_key=run_key, verbose=args.verbose
                )
            await db.commit()

            if args.similarity:
                from app.cli.compute_similarity import (
                    SimilarityConfig,
                    compute_for_snapshot,
                )

                config = SimilarityConfig()
                seen_snapshot_ids: Set[int] = set()
                contexts_for_similarity = {ctx for ctx, _ in promotion_jobs}
                for context in contexts_for_similarity:
                    sid = await _find_current_snapshot_id(db, context)
                    if sid is None:
                        if args.verbose:
                            print(
                                f"[similarity] No current snapshot for "
                                f"cohort={context.cohort.value} "
                                f"source={context.source.value}"
                            )
                        continue
                    if sid in seen_snapshot_ids:
                        continue
                    seen_snapshot_ids.add(sid)
                    if args.verbose:
                        print(
                            f"[similarity] Computing for snapshot_id={sid} "
                            f"cohort={context.cohort.value} "
                            f"source={context.source.value}"
                        )
                    await compute_for_snapshot(db, sid, config)
        else:
            await db.rollback()


def main(argv: Optional[Sequence[str]] = None) -> None:
    asyncio.run(run_recompute(argv))


if __name__ == "__main__":
    main()
