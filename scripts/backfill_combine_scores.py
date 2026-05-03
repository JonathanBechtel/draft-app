#!/usr/bin/env python
"""Backfill composite combine scores end-to-end.

Iterates ``app.cli.compute_combine_scores`` over every ``(cohort, season)`` pair
the upstream combine data covers (using the parent position-scope matrix), then
promotes each newly created snapshot to ``is_current=true``. The CLI itself
writes new snapshots with ``is_current=false`` and there is no built-in
promotion path for combine_score, so the promotion step here is required to
make the page render.

Reads ``DATABASE_URL`` from the environment (env vars take precedence over
``.env`` per pydantic-settings). Run against prod by setting
``DATABASE_URL=<prod url>`` on the command line.

Usage:
    DATABASE_URL=<prod-url> python scripts/backfill_combine_scores.py --dry-run
    DATABASE_URL=<prod-url> python scripts/backfill_combine_scores.py --execute
"""

from __future__ import annotations

import argparse
import asyncio
from typing import List, Optional, Tuple

from dotenv import load_dotenv
from sqlalchemy import text

load_dotenv()

# Imports below depend on DATABASE_URL being resolved first; load_dotenv must
# run before the engine is constructed at module import time.
from app.cli.compute_combine_scores import (  # noqa: E402
    main_async as compute_combine_scores_main,
)
from app.models.fields import CohortType  # noqa: E402
from app.utils.db_async import SessionLocal  # noqa: E402

# Same defaults as recompute_metrics.GLOBAL_COHORTS_DEFAULT.
GLOBAL_COHORTS: List[CohortType] = [
    CohortType.global_scope,
    CohortType.all_time_draft,
    CohortType.current_nba,
    CohortType.all_time_nba,
]


async def discover_seasons() -> List[Tuple[int, str]]:
    """Find every season that has at least one combine input row.

    Mirrors the season-discovery loop in ``recompute_metrics._resolve_season_ids``
    so we backfill exactly the seasons the upstream pipeline knows about.
    """
    async with SessionLocal() as db:
        result = await db.execute(
            text(
                """
                SELECT s.id, s.code
                FROM seasons s
                WHERE s.id IN (
                    SELECT season_id FROM combine_anthro WHERE season_id IS NOT NULL
                    UNION
                    SELECT season_id FROM combine_agility WHERE season_id IS NOT NULL
                    UNION
                    SELECT season_id FROM combine_shooting_results
                    WHERE season_id IS NOT NULL
                )
                ORDER BY s.start_year
                """
            )
        )
        return [(row[0], row[1]) for row in result.all()]


async def run_compute(
    cohort: CohortType,
    season_code: Optional[str],
    *,
    dry_run: bool,
    verbose: bool,
) -> None:
    """Invoke compute_combine_scores for one cohort/season across all parent scopes."""
    argv: List[str] = [
        "--cohort",
        cohort.value,
        "--position-matrix",
        "parent",
        "--replace-run",
    ]
    if season_code:
        argv.extend(["--season", season_code])
    if dry_run:
        argv.append("--dry-run")
    if verbose:
        argv.append("--verbose")

    label = cohort.value + (f" / {season_code}" if season_code else "")
    print(f"  -> {label}")
    await compute_combine_scores_main(argv)


async def promote_snapshots() -> Tuple[int, int]:
    """Promote the latest combine_score snapshot per context to is_current=true.

    Two-step (demote-then-promote) to avoid transient violations of the partial
    unique index ``uq_metric_snapshots_current``. Mirrors the canonical pattern
    in ``recompute_metrics._promote_snapshot``.

    Returns:
        Tuple of (demoted_count, promoted_count).
    """
    async with SessionLocal() as db:
        async with db.begin():
            demoted = await db.execute(
                text(
                    "UPDATE metric_snapshots SET is_current = false "
                    "WHERE source = 'combine_score' AND is_current = true"
                )
            )
            promoted = await db.execute(
                text(
                    """
                    UPDATE metric_snapshots
                    SET is_current = true
                    WHERE id IN (
                        SELECT DISTINCT ON (
                            cohort, season_id, position_scope_parent, position_scope_fine
                        ) id
                        FROM metric_snapshots
                        WHERE source = 'combine_score'
                        ORDER BY
                            cohort,
                            season_id,
                            position_scope_parent,
                            position_scope_fine,
                            version DESC
                    )
                    """
                )
            )
        return demoted.rowcount, promoted.rowcount


async def summarize_state() -> None:
    """Print a quick summary of combine_score rows currently in the target DB."""
    async with SessionLocal() as db:
        r = await db.execute(
            text(
                "SELECT COUNT(*) FROM metric_snapshots "
                "WHERE source = 'combine_score'"
            )
        )
        total = r.scalar()
        r = await db.execute(
            text(
                "SELECT COUNT(*) FROM metric_snapshots "
                "WHERE source = 'combine_score' AND is_current = true"
            )
        )
        current = r.scalar()
        print(f"  combine_score snapshots: total={total}, is_current={current}")


async def main(*, dry_run: bool, verbose: bool) -> None:
    print("Initial state:")
    await summarize_state()

    seasons = await discover_seasons()
    print(
        f"\nDiscovered {len(seasons)} season(s) with combine data: "
        f"{[code for _, code in seasons]}"
    )

    print("\n=== Computing combine scores ===")
    for cohort in GLOBAL_COHORTS:
        season = "all" if cohort == CohortType.global_scope else None
        await run_compute(cohort, season, dry_run=dry_run, verbose=verbose)
    for _sid, code in seasons:
        await run_compute(
            CohortType.current_draft, code, dry_run=dry_run, verbose=verbose
        )

    if dry_run:
        print("\n[dry-run] Skipping promotion. No data persisted.")
        return

    print("\n=== Promoting snapshots ===")
    demoted, promoted = await promote_snapshots()
    print(f"  demoted: {demoted}, promoted: {promoted}")

    print("\nFinal state:")
    await summarize_state()


def cli() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill combine_score snapshots end-to-end (compute + promote)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute without persisting and skip promotion",
    )
    parser.add_argument("--verbose", action="store_true", help="Verbose CLI output")
    args = parser.parse_args()
    asyncio.run(main(dry_run=args.dry_run, verbose=args.verbose))


if __name__ == "__main__":
    cli()
