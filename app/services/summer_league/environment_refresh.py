"""Incremental Competition Context (#617) refresh orchestration for the pipeline.

Wires the frozen aggregation/publication contract
(``docs/plans/competition-context-explorer-implementation-contract.md`` §8)
into the production Summer League ingest pipeline and into standalone
recovery. Two entry points:

* :func:`refresh_environment_profiles_for_year` -- the pipeline-invoked
  incremental refresh. Must be called from inside the transaction that
  already holds the Summer League writer lock (the ingest runner's locked
  ``metrics_and_snapshots`` phase, *after* normalized facts and advanced
  metrics are materialized -- see
  ``app/cli/summer_league_ingest_runner.py``). Delegates to
  :func:`app.services.summer_league_environment_service.rebuild_environment_profiles`,
  which acquires/re-enters that same lock as its own first action, so this
  wrapper adds telemetry, durable outcome recording, and failure isolation
  around the shared aggregation contract without duplicating it.
* :func:`rollback_environment_profile` -- a standalone recovery operation
  (opens/holds its own locked transaction) that flips ``is_current`` back to
  an already-published prior version for one scope, for the "roll back to a
  prior current version" runbook operation. Distinct from a rebuild-time
  validation failure (#617), which already preserves the prior current
  profile automatically -- this reverses an already-*published*,
  since-judged-wrong version.

Also defines the public staleness contract (§8's "a profile beyond the
configured freshness threshold ... displays a stale badge"):
:func:`is_environment_profile_stale` is the single source of truth other
surfaces (#607/#608 reads) should call rather than re-deriving the threshold.

Durable run outcome is recorded on the existing
:class:`~app.schemas.summer_league_pipeline.SummerLeaguePipelineState` table
under the ``environment_refresh`` job (see
:mod:`app.services.ingest.pipeline_state`) -- the same
success/failure/timestamp machinery ``desk``/``full_ingestion`` already use,
per "keep operational state centralized." This is how a failed incremental
refresh stays "visible and retryable" (contract §8) without erasing the last
good profile or corrupting normalized facts: a failure here is caught,
logged, and recorded, but never re-raised into the caller's surrounding
pipeline transaction.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.schemas.summer_league_environment import SummerLeagueEnvironmentProfile
from app.schemas.summer_league_pipeline import SummerLeaguePipelineJob
from app.services.ingest.pipeline_state import (
    complete_pipeline,
    record_pipeline_failure,
)
from app.services.ingest.pipeline_telemetry import PipelineTelemetry
from app.services.ingest.write_lock import acquire_summer_league_writer_lock
from app.services.summer_league_environment_registry import is_profile_stale
from app.services.summer_league_environment_service import (
    EnvironmentRebuildResult,
    rebuild_environment_profiles,
)

logger = logging.getLogger(__name__)

_JOB = SummerLeaguePipelineJob.ENVIRONMENT_REFRESH


@dataclass
class EnvironmentRefreshOutcome:
    """Structured summary of one pipeline-invoked incremental refresh.

    Mirrors :class:`~app.services.summer_league_environment_service.EnvironmentRebuildResult`
    (the underlying #617 aggregation contract's own report) plus the
    pipeline-facing framing this orchestration layer adds: whether a refresh
    was attempted at all this cycle, whether it fully succeeded, and any
    top-level error that aborted the rebuild call itself (as opposed to a
    per-scope validation failure, which ``failures`` already carries).
    """

    year: int
    attempted: bool = False
    succeeded: bool = False
    requested_scopes: int = 0
    built_scopes: int = 0
    skipped_scopes: int = 0
    failed_scopes: int = 0
    metric_coverage_complete: int = 0
    calculation_version: Optional[str] = None
    input_watermark: Optional[datetime] = None
    duration_seconds: float = 0.0
    published_scope_keys: list[str] = field(default_factory=list)
    failures: dict[str, str] = field(default_factory=dict)
    error: Optional[str] = None


def resolve_environment_refresh_scope(
    *, year: int, any_games: bool, pending_reconciliation: bool
) -> Optional[int]:
    """Decide which year's Competition Context profiles to refresh this cycle.

    The ingest runner processes exactly one Summer League season year per
    run (``SL_INGEST_YEAR``), so "incremental scope selection" here is a
    single go/no-go decision rather than a set of competitions to pick
    between: refresh that year (season scope + every competition scope in
    it, per the #617 aggregation contract) when this cycle touched
    normalized facts (``any_games``) or is draining a previously deferred
    reconciliation (``pending_reconciliation``); otherwise leave the
    currently published profiles untouched rather than republishing an
    identical version. Named and tested independently of the (unrelated)
    metrics/snapshot-materialization gate the ingest runner also uses, even
    though both currently share the same two inputs -- the two gates guard
    different phases and could diverge later.

    Args:
        year: The Summer League season year this cycle processed.
        any_games: Whether at least one venue discovered games this cycle.
        pending_reconciliation: Whether a prior cycle deferred work that
            still needs draining.

    Returns:
        ``year`` when a refresh is warranted this cycle; ``None`` to skip.
    """
    if any_games or pending_reconciliation:
        return year
    return None


async def refresh_environment_profiles_for_year(
    db: AsyncSession,
    *,
    year: int,
    telemetry: Optional[PipelineTelemetry] = None,
) -> EnvironmentRefreshOutcome:
    """Incrementally refresh this year's Competition Context profiles.

    Must be called from inside the caller's already-open, already-locked
    transaction (the ingest runner's locked ``metrics_and_snapshots`` phase,
    reusing that session per contract §8 point 4) -- this function does not
    open its own transaction.
    :func:`~app.services.summer_league_environment_service.rebuild_environment_profiles`
    acquires the transaction-scoped writer lock as its own first action; the
    advisory lock is re-entrant, so calling it from an already-locked
    transaction is safe and adds no extra wait.

    Failure isolation: any exception raised while rebuilding (an unexpected
    error inside the aggregation service, a DB error, etc.) is caught,
    logged with full context, and recorded via the durable pipeline-state
    row for the ``environment_refresh`` job. It never re-raises: a broken
    Competition Context refresh must not roll back the normalized-fact and
    advanced-metrics work this same transaction just committed, and the last
    good profile stays published and readable (contract §8) until a later
    run or a manual recovery (see the runbook) succeeds. A *partial* result
    (some scopes built, some failed validation) is not re-raised either --
    that isolation already happens one layer down, inside
    ``rebuild_environment_profiles`` itself, which is why a per-scope
    validation failure never prevents its sibling scopes from publishing.

    Args:
        db: The caller's open, locked session.
        year: The Summer League season year whose facts were just touched.
        telemetry: Optional structured-timing recorder shared with the rest
            of the pipeline run.

    Returns:
        An :class:`EnvironmentRefreshOutcome` describing what happened.
    """
    outcome = EnvironmentRefreshOutcome(year=year, attempted=True)
    started = datetime.now(timezone.utc)

    try:
        if telemetry is not None:
            with telemetry.step(f"environment_refresh:year:{year}"):
                result = await rebuild_environment_profiles(db, year=year)
        else:
            result = await rebuild_environment_profiles(db, year=year)
    except Exception as exc:  # noqa: BLE001 -- isolate any refresh failure
        outcome.duration_seconds = (
            datetime.now(timezone.utc) - started
        ).total_seconds()
        outcome.error = f"{type(exc).__name__}: {exc}"
        logger.error(
            "environment_refresh_run job=%s year=%s outcome=failed error=%s "
            "duration_ms=%s",
            _JOB.value,
            year,
            outcome.error,
            round(outcome.duration_seconds * 1000, 1),
            exc_info=True,
        )
        await record_pipeline_failure(db, job=_JOB, reason=outcome.error)
        return outcome

    _apply_result(outcome, result)
    outcome.succeeded = not outcome.failures
    outcome.duration_seconds = (datetime.now(timezone.utc) - started).total_seconds()

    logger.info(
        "environment_refresh_run job=%s year=%s outcome=%s requested=%d built=%d "
        "skipped=%d failed=%d coverage_complete=%d calculation_version=%s "
        "input_watermark=%s duration_ms=%s",
        _JOB.value,
        year,
        "succeeded" if outcome.succeeded else "partial_failure",
        outcome.requested_scopes,
        outcome.built_scopes,
        outcome.skipped_scopes,
        outcome.failed_scopes,
        outcome.metric_coverage_complete,
        outcome.calculation_version,
        outcome.input_watermark,
        round(outcome.duration_seconds * 1000, 1),
    )
    for scope_key, reason in outcome.failures.items():
        logger.error(
            "environment_refresh_scope_failed job=%s year=%s scope_key=%s reason=%s",
            _JOB.value,
            year,
            scope_key,
            reason,
        )

    if outcome.succeeded:
        await complete_pipeline(
            db, job=_JOB, metrics_rebuilt=False, snapshots_materialized=False
        )
    else:
        await record_pipeline_failure(
            db,
            job=_JOB,
            reason=(
                f"{outcome.failed_scopes} of {outcome.requested_scopes} "
                f"scope(s) failed validation: {outcome.failures}"
            ),
        )
    return outcome


def _apply_result(
    outcome: EnvironmentRefreshOutcome, result: EnvironmentRebuildResult
) -> None:
    """Copy the #617 aggregation contract's result onto the pipeline-facing outcome."""
    outcome.requested_scopes = result.requested_scopes
    outcome.built_scopes = result.built_scopes
    outcome.skipped_scopes = result.skipped_scopes
    outcome.failed_scopes = result.failed_scopes
    outcome.metric_coverage_complete = result.metric_coverage_complete
    outcome.calculation_version = result.calculation_version
    outcome.input_watermark = result.input_watermark
    outcome.published_scope_keys = list(result.published_scope_keys)
    outcome.failures = dict(result.failures)


def is_environment_profile_stale(
    calculated_at: datetime, *, now: Optional[datetime] = None
) -> bool:
    """Whether a profile's last computation exceeds the configured freshness threshold.

    Thin re-export of :func:`app.services.summer_league_environment_registry.
    is_profile_stale` (the actual single source of truth, callable from every
    public surface without a circular import back into this pipeline module).
    Kept here under its original name for existing call sites/tests.

    Args:
        calculated_at: The profile's ``SummerLeagueEnvironmentProfile.calculated_at``.
        now: Reference instant; defaults to the real current time (UTC).

    Returns:
        ``True`` once ``now - calculated_at`` exceeds
        ``settings.summer_league_environment_stale_after_hours``.
    """
    return is_profile_stale(calculated_at, now=now)


@dataclass
class EnvironmentRollbackResult:
    """Outcome of a manual "restore a prior current version" recovery operation."""

    scope_key: str
    previous_current_version: Optional[int]
    restored_version: int
    changed: bool


async def rollback_environment_profile(
    db: AsyncSession, *, scope_key: str, target_version: int
) -> EnvironmentRollbackResult:
    """Atomically restore an already-published prior version to current.

    Standalone recovery operation (runbook "rollback to a prior current
    version"): opens no transaction of its own -- the caller wraps this in
    ``async with db.begin():``, exactly like the standalone rebuild script
    (contract §8 point 4, "a manual or standalone rebuild opens its own
    transaction and acquires the same lock before its first input read").
    Acquires the shared writer lock first for the same reason a rebuild
    does: a concurrent rebuild must never race this switch, and a demote
    followed by a promote must land in one commit (mirrors
    ``_publish_candidate``'s demote-then-flush-then-promote pattern in
    ``app.services.summer_league_environment_service``, which this
    deliberately does not import from -- it operates on the profile schema
    directly rather than depending on that module's private helpers).

    Args:
        db: Async session whose transaction will publish the switch.
        scope_key: The stable scope key (``season:<year>`` or
            ``competition:<competition_id>``) to roll back.
        target_version: The version number to restore as current. Must
            already exist for ``scope_key``.

    Returns:
        The result of the switch, including whether anything changed (a
        no-op when ``target_version`` is already current).

    Raises:
        ValueError: No versions exist for ``scope_key``, or
            ``target_version`` does not exist for it.
    """
    await acquire_summer_league_writer_lock(db)

    rows = (
        (
            await db.execute(
                select(SummerLeagueEnvironmentProfile).where(
                    col(SummerLeagueEnvironmentProfile.scope_key) == scope_key
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        raise ValueError(f"no profile versions exist for scope_key={scope_key!r}")

    target = next((row for row in rows if row.version == target_version), None)
    if target is None:
        raise ValueError(f"scope_key={scope_key!r} has no version {target_version}")

    current = next((row for row in rows if row.is_current), None)
    previous_version = current.version if current is not None else None

    if current is not None and current.id == target.id:
        return EnvironmentRollbackResult(
            scope_key=scope_key,
            previous_current_version=previous_version,
            restored_version=target_version,
            changed=False,
        )

    now = datetime.utcnow()
    if current is not None:
        # Demote first: a partial unique index guarantees at most one
        # is_current=true row per scope_key, so the demotion must be
        # flushed before the promotion to avoid ever observing two current
        # rows within this transaction.
        current.is_current = False
        current.updated_at = now
        await db.flush()

    target.is_current = True
    target.updated_at = now
    await db.flush()

    logger.info(
        "environment_profile_rollback scope_key=%s previous_version=%s "
        "restored_version=%s",
        scope_key,
        previous_version,
        target_version,
    )
    return EnvironmentRollbackResult(
        scope_key=scope_key,
        previous_current_version=previous_version,
        restored_version=target_version,
        changed=True,
    )
