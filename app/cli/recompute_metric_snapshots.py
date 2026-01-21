from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from typing import List, Optional, Sequence

from sqlalchemy import select

from app.cli.compute_metrics import main_async as compute_metrics_main
from app.models.fields import CohortType, MetricSource
from app.schemas.metrics import MetricSnapshot
from app.schemas.seasons import Season
from app.utils.db_async import SessionLocal, load_schema_modules


@dataclass(frozen=True)
class ComputeJob:
    argv: List[str]
    command: str


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Recompute metric snapshots for the same season/cohort matrix already present "
            "in the database. Default mode prints the compute_metrics commands; pass "
            "--execute to actually run them."
        )
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Run compute_metrics (omit to only print the commands).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Append --dry-run to compute_metrics commands (no database writes).",
    )
    parser.add_argument(
        "--cohorts",
        nargs="+",
        choices=[c.value for c in CohortType],
        default=[c.value for c in CohortType],
        help="Cohorts to include (default: all cohorts).",
    )
    parser.add_argument(
        "--sources",
        nargs="+",
        choices=[s.value for s in MetricSource],
        help=(
            "Metric sources to include (e.g., combine_anthro combine_agility). "
            "Default: use the distinct sources already present in metric_snapshots."
        ),
    )
    parser.add_argument(
        "--season-codes",
        nargs="+",
        help=(
            "Limit current_draft recompute to these season codes (e.g., 2024-25 2025-26). "
            "By default, uses the season codes already present in metric_snapshots."
        ),
    )
    parser.add_argument(
        "--min-sample",
        type=int,
        default=5,
        help="Minimum sample size for non-global cohorts (default: 5).",
    )
    parser.add_argument(
        "--global-season",
        default="all",
        help="Season code passed to the global_scope run (default: all).",
    )
    parser.add_argument(
        "--global-min-sample",
        type=int,
        default=3,
        help="Minimum sample size for the global_scope run (default: 3).",
    )
    return parser.parse_args(argv)


def _cmd_for(argv_inner: Sequence[str]) -> str:
    parts = ["python -m app.cli.compute_metrics", *argv_inner]
    return " ".join(parts)


async def _current_draft_season_codes(
    *, session, explicit_codes: Optional[Sequence[str]]
) -> List[str]:
    if explicit_codes:
        return list(explicit_codes)

    stmt = (
        select(Season.code)
        .join(MetricSnapshot, MetricSnapshot.season_id == Season.id)
        .where(MetricSnapshot.cohort == CohortType.current_draft)  # type: ignore[arg-type]
        .distinct()
        .order_by(Season.code)
    )
    result = await session.execute(stmt)
    return [row[0] for row in result.all()]


async def _snapshot_sources(
    *, session, explicit_sources: Optional[Sequence[str]]
) -> List[str]:
    if explicit_sources:
        return list(explicit_sources)

    stmt = select(MetricSnapshot.source).distinct().order_by(MetricSnapshot.source)
    result = await session.execute(stmt)
    sources: List[str] = []
    for (source,) in result.all():
        if isinstance(source, MetricSource):
            sources.append(source.value)
        else:
            sources.append(str(source))
    return sources


async def build_jobs(args: argparse.Namespace) -> List[ComputeJob]:
    cohorts = {CohortType(c) for c in args.cohorts}
    jobs: List[ComputeJob] = []

    async with SessionLocal() as session:
        season_codes = await _current_draft_season_codes(
            session=session, explicit_codes=args.season_codes
        )
        sources = await _snapshot_sources(
            session=session, explicit_sources=args.sources
        )

    def add_job(argv_inner: List[str]) -> None:
        if sources:
            argv_inner = [*argv_inner, "--sources", *sources]
        if args.dry_run:
            argv_inner = [*argv_inner, "--dry-run"]
        jobs.append(ComputeJob(argv=argv_inner, command=_cmd_for(argv_inner)))

    # current_draft is season-scoped
    if CohortType.current_draft in cohorts:
        for season_code in season_codes:
            add_job(
                [
                    "--cohort",
                    CohortType.current_draft.value,
                    "--season",
                    season_code,
                    "--position-matrix",
                    "parent",
                    "--min-sample",
                    str(args.min_sample),
                ]
            )
            add_job(
                [
                    "--cohort",
                    CohortType.current_draft.value,
                    "--season",
                    season_code,
                    "--position-matrix",
                    "fine",
                    "--matrix-skip-baseline",
                    "--min-sample",
                    str(args.min_sample),
                ]
            )

    # Other cohorts are seasonless (and align to baseline+parent+fine in the existing DB)
    for cohort in (
        CohortType.all_time_draft,
        CohortType.current_nba,
        CohortType.all_time_nba,
    ):
        if cohort not in cohorts:
            continue
        add_job(
            [
                "--cohort",
                cohort.value,
                "--position-matrix",
                "parent",
                "--min-sample",
                str(args.min_sample),
            ]
        )
        add_job(
            [
                "--cohort",
                cohort.value,
                "--position-matrix",
                "fine",
                "--matrix-skip-baseline",
                "--min-sample",
                str(args.min_sample),
            ]
        )

    # global_scope snapshots in the DB are baseline-only.
    if CohortType.global_scope in cohorts:
        add_job(
            [
                "--cohort",
                CohortType.global_scope.value,
                "--season",
                args.global_season,
                "--min-sample",
                str(args.global_min_sample),
            ]
        )

    return jobs


async def run(argv: Optional[Sequence[str]] = None) -> None:
    args = parse_args(argv)
    load_schema_modules()

    jobs = await build_jobs(args)
    if not jobs:
        print("No metric snapshot jobs selected.")
        return

    for job in jobs:
        print(job.command)
        if args.execute:
            await compute_metrics_main(job.argv)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
