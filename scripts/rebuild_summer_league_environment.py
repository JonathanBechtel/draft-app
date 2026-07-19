"""Rebuild Competition Context (Summer League environment) profiles.

Deterministic, idempotent, set-based rebuild of the versioned season and
competition profiles from the normalized Summer League spokes. Three modes:

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

Run:
  scripts/with-db-env.sh conda run -n draftguru \
    python scripts/rebuild_summer_league_environment.py [--year 2025] \
    [--competition-id 12]
"""

from __future__ import annotations

import argparse
import asyncio

from app.services.summer_league_environment_service import (
    EnvironmentRebuildResult,
    rebuild_environment_profiles,
)
from app.utils.db_async import SessionLocal, engine


async def _run(
    *, year: int | None, competition_id: int | None
) -> EnvironmentRebuildResult:
    async with SessionLocal() as db:
        async with db.begin():
            return await rebuild_environment_profiles(
                db, year=year, competition_id=competition_id
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
    return parser.parse_args()


async def main() -> None:
    args = _parse_args()
    try:
        result = await rebuild_wrapper(
            year=args.year, competition_id=args.competition_id
        )
    finally:
        await engine.dispose()
    print(
        "Rebuilt Competition Context profiles: "
        f"{result.built_scopes} built, {result.skipped_scopes} skipped, "
        f"{result.failed_scopes} failed of {result.requested_scopes} requested "
        f"(registry {result.registry_version}); "
        f"{result.metric_coverage_complete} complete metric coverages; "
        f"watermark={result.input_watermark}; "
        f"{result.duration_seconds:.2f}s."
    )
    if result.failures:
        for scope_key, reason in result.failures.items():
            print(f"  FAILED {scope_key}: {reason}")


async def rebuild_wrapper(
    *, year: int | None, competition_id: int | None
) -> EnvironmentRebuildResult:
    """Thin async wrapper so ``main`` can dispose the engine in a ``finally``."""
    return await _run(year=year, competition_id=competition_id)


if __name__ == "__main__":
    asyncio.run(main())
