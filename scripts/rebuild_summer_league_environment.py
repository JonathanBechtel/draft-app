"""Rebuild or roll back Competition Context (Summer League environment) profiles.

Deterministic, idempotent, set-based rebuild of the versioned season and
competition profiles from the normalized Summer League spokes. Three rebuild
modes:

* ``--competition-id N`` — rebuild exactly one competition scope.
* ``--year Y`` — rebuild that year's all-competitions season scope plus every
  competition scope in the year.
* neither — full historical rebuild of every competition and season scope.

The command opens one transaction and the aggregation service acquires the
shared Summer League writer lock **before** its first source read, holding that
same transaction through calculation, validation, version insertion, and the
atomic current-version switch (implementation contract §8). Raw facts are never
mutated; a failed candidate leaves the prior current profile in place. Re-running
is safe — each run publishes a fresh version and flips ``is_current`` atomically.

A fourth, mutually exclusive mode recovers from a bad publish by restoring an
already-published prior version to current, rather than recomputing anything:

* ``--rollback-scope-key KEY --rollback-to-version N`` — atomically flips
  ``is_current`` back to version ``N`` of scope ``KEY`` (see
  ``docs/summer_league_environment_profiles_runbook.md``).

Run:
  scripts/with-db-env.sh conda run -n draftguru \
    python scripts/rebuild_summer_league_environment.py [--year 2025] \
    [--competition-id 12]

  scripts/with-db-env.sh conda run -n draftguru \
    python scripts/rebuild_summer_league_environment.py \
    --rollback-scope-key season:2025 --rollback-to-version 3
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Optional

from app.services.sources.summer_league.environment_refresh import (
    EnvironmentRollbackResult,
    rollback_environment_profile,
)
from app.services.summer_league_environment_service import (
    EnvironmentRebuildResult,
    rebuild_environment_profiles,
)
from app.utils.db_async import SessionLocal, engine


async def _run(
    *, year: Optional[int], competition_id: Optional[int]
) -> EnvironmentRebuildResult:
    async with SessionLocal() as db:
        async with db.begin():
            return await rebuild_environment_profiles(
                db, year=year, competition_id=competition_id
            )


async def _run_rollback(
    *, scope_key: str, target_version: int
) -> EnvironmentRollbackResult:
    async with SessionLocal() as db:
        async with db.begin():
            return await rollback_environment_profile(
                db, scope_key=scope_key, target_version=target_version
            )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument(
        "--year",
        type=int,
        default=None,
        help="Rebuild this year's season + competition scopes.",
    )
    scope.add_argument(
        "--competition-id",
        type=int,
        default=None,
        help="Rebuild exactly this competition scope.",
    )
    parser.add_argument(
        "--rollback-scope-key",
        type=str,
        default=None,
        help=(
            "Restore an already-published prior version to current for this "
            "scope key ('season:<year>' or 'competition:<id>'); requires "
            "--rollback-to-version. Mutually exclusive with --year/--competition-id."
        ),
    )
    parser.add_argument(
        "--rollback-to-version",
        type=int,
        default=None,
        help="The version number to restore as current (see --rollback-scope-key).",
    )
    args = parser.parse_args()
    if (args.rollback_scope_key is None) != (args.rollback_to_version is None):
        parser.error(
            "--rollback-scope-key and --rollback-to-version must be given together"
        )
    if args.rollback_scope_key is not None and (
        args.year is not None or args.competition_id is not None
    ):
        parser.error(
            "--rollback-scope-key is mutually exclusive with --year/--competition-id"
        )
    return args


async def main() -> None:
    args = _parse_args()
    try:
        if args.rollback_scope_key is not None:
            rollback_result = await rollback_wrapper(
                scope_key=args.rollback_scope_key,
                target_version=args.rollback_to_version,
            )
            _print_rollback_result(rollback_result)
            return
        result = await rebuild_wrapper(
            year=args.year, competition_id=args.competition_id
        )
    finally:
        await engine.dispose()
    print(
        "Rebuilt Competition Context profiles: "
        f"{result.built_scopes} built, {result.skipped_scopes} skipped, "
        f"{result.failed_scopes} failed of {result.requested_scopes} requested "
        f"(registry {result.registry_version}, calc {result.calculation_version}); "
        f"{result.metric_coverage_complete} complete metric coverages; "
        f"watermark={result.input_watermark}; "
        f"{result.duration_seconds:.2f}s."
    )
    if result.failures:
        for scope_key, reason in result.failures.items():
            print(f"  FAILED {scope_key}: {reason}")


def _print_rollback_result(result: EnvironmentRollbackResult) -> None:
    if not result.changed:
        print(
            f"No change: version {result.restored_version} was already current "
            f"for scope_key={result.scope_key}."
        )
        return
    print(
        f"Rolled back scope_key={result.scope_key}: version "
        f"{result.previous_current_version} -> {result.restored_version} is now current."
    )


async def rebuild_wrapper(
    *, year: Optional[int], competition_id: Optional[int]
) -> EnvironmentRebuildResult:
    """Thin async wrapper so ``main`` can dispose the engine in a ``finally``."""
    return await _run(year=year, competition_id=competition_id)


async def rollback_wrapper(
    *, scope_key: str, target_version: int
) -> EnvironmentRollbackResult:
    """Thin async wrapper so ``main`` can dispose the engine in a ``finally``."""
    return await _run_rollback(scope_key=scope_key, target_version=target_version)


if __name__ == "__main__":
    asyncio.run(main())
