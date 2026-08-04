"""Standalone cron runner for scheduled Summer League game-data ingestion.

This script is designed to run as a scheduled Fly.io machine, executing an
incremental Summer League raw-fetch -> backbone -> advanced-metrics pipeline
directly against the database, without going through the HTTP API.

For each configured venue (NBA Stats LeagueID) it:

1. Refreshes the season index only (``leaguegamelog`` re-fetch, no per-game
   downloads) so newly scheduled/played games become visible.
2. Fetches any newly-appeared per-game files; already-downloaded games are
   skipped so this stays cheap on every run.
3. If the venue has zero games this run (pre-tip-off, or between events),
   logs and skips the backbone/normalization stages for that venue entirely
   -- ``backfill_summer_league_backbone`` raises when there are no raw
   manifests to audit, so this gate is required for safety.
4. Otherwise runs the audit -> normalize -> resolve backbone, then shot and
   play-by-play normalization, for that venue.

After every configured venue has been attempted, if *any* venue had games
this run, the global advanced-metrics table is rebuilt once (it is a full
wipe+rebuild, not scoped to one venue/year).

5. Refreshes the active Summer League event's *forward* schedule (the
   ``scheduleleaguev2`` feed, via
   ``app.services.summer_league.scoreboard_ingest.run_scoreboard_ingest``)
   so ``summer_league_games.tip_datetime`` stays fresh at this cron's own
   hourly cadence, decoupled from the Summer League Desk tick
   (``app/cli/sl_desk_tick.py``) -- previously the *only* place that feed
   was ever polled, and only once the Desk itself was already non-dormant.
   That was a chicken-and-egg gap: the Desk can't wake up without tip
   times, and it can't get tip times because it's asleep. This step reuses
   the per-venue NBA Stats client already opened above (no second client)
   and is gated by a window guard (:func:`_schedule_pull_in_window`) so it
   never calls stats.nba.com during the long off-season -- see that
   function's docstring for exactly how the window is resolved. Additive
   and best-effort: a failure here is logged and does not fail the run, and
   the Desk tick's own step-0 scoreboard ingest is left untouched as
   belt-and-suspenders.

One venue failing does not abort the others. Fetch-stage errors (e.g.
stats.nba.com being unreachable) are treated the same as "no games yet" for
that venue and do not fail the run -- there is nothing to process either way.
Only a genuine failure *after* games were detected (backbone/normalization/
metrics errors) marks the run as failed. The schedule refresh (step 5) is
independently best-effort for the same reason -- see its own docstring.

Usage:
    python -m app.cli.summer_league_ingest_runner

Environment overrides:
    SL_INGEST_YEAR - four-digit Summer League year (default: 2026)
    SL_INGEST_LEAGUE_IDS - comma-separated NBA Stats LeagueIDs
        (default: "13,16,15")
    SL_INGEST_FULL_RECONCILE - when set to a truthy value ("1", "true",
        "yes"), forces a complete shot/PBP reprocess for every venue this
        run by clearing all of that venue/year's durable batch-progress
        rows (see app.services.ingest.batch_progress) before the
        batched phases run, bypassing the "already completed, skip it"
        filter entirely. An operator escape hatch for repair -- e.g. after
        a raw-snapshot backfill/migration that could have silently changed
        already-normalized games without this module's ordinary dirty-game
        detection (dirty_game_ids_from_manifest) ever seeing it happen.
        Default: unset (routine incremental runs rely on dirty detection
        alone).

Exit codes:
    0 - Success, including the pre-tip-off/no-games no-op case
    1 - Failure (check logs for details)
"""

# discipline: file-size orchestration-only reconciliation and lock-boundary wiring

import asyncio
import logging
import os
import sys
from collections.abc import Awaitable, Callable, Sequence
from contextlib import nullcontext
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.event_desk import EventLifecyclePhase
from app.schemas.summer_league import SummerLeagueEdition
from app.services.event_desk.lifecycle import lifecycle_phase
from app.services.event_desk.registry import DeskEvent, SUMMER_LEAGUE_REGISTRATION
from app.services.event_desk.timeutils import to_eastern_date
from app.services.player_identity_guard import (
    IdentityDuplicateAuditReport,
    audit_variant_player_duplicates,
)
from app.services.summer_league.backfill import (
    SummerLeagueBackfillOptions,
    backfill_summer_league_backbone,
    summarize_backfill_report,
)
from app.services.summer_league.endpoints import normalize_league_id
from app.services.summer_league.manifest import SummerLeagueRawManifest
from app.services.summer_league.metrics_rebuild_gate import (
    MetricsStageContext,
    run_metrics_stage,
)
from app.services.summer_league.player_resolution import (
    SummerLeagueResolutionReport,
    SummerLeagueResolutionResult,
    apply_source_player_resolution_plan,
    build_resolution_report,
    prepare_summer_league_player_resolutions,
    revalidate_source_player_resolution_plan,
)
from app.services.summer_league.nba_stats_client import NBAStatsClient
from app.services.ingest.batch_progress import (
    count_pending_batch_games,
    get_completed_batch_game_ids,
    invalidate_batch_progress,
    record_batch_progress,
)
from app.services.summer_league.normalization import (
    SummerLeaguePBPEventReport,
    SummerLeagueShotEventReport,
    chunked,
    find_incomplete_team_box_game_ids,
    normalize_competition_games,
    normalize_pbp_events,
    normalize_shot_events,
)
from app.services.ingest.pipeline_state import (
    defer_full_reconciliation,
    record_pipeline_failure,
)
from app.services.ingest.pipeline_telemetry import PipelineTelemetry
from app.services.summer_league.raw_ingestion import (
    RawIngestionOptions,
    SummerLeagueRawIngestor,
    dirty_game_ids_from_manifest,
)
from app.services.summer_league.raw_store import SummerLeagueRawStore
from app.services.summer_league.scoreboard_ingest import (
    ScoreboardIngestReport,
    resolve_target_competitions,
    run_scoreboard_ingest,
)
from app.services.ingest.write_lock import (
    try_acquire_summer_league_writer_lock,
    try_acquire_summer_league_writer_lock_yielding,
)
from app.schemas.summer_league_pipeline import (
    SummerLeagueBatchPhase,
    SummerLeaguePipelineJob,
)
from app.utils.db_async import SessionLocal, dispose_engine

# Configure logging for cron context
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("summer_league_ingest_runner")

DEFAULT_YEAR = 2026
DEFAULT_LEAGUE_IDS = ("13", "16", "15")
RAW_ROOT = Path("data/raw/nba_stats/summer_league")

# Mirrors scripts/fetch_summer_league_raw.py defaults so cron traffic looks
# the same as a manual/local invocation to stats.nba.com.
FETCH_TIMEOUT_SECONDS = 30.0
FETCH_DELAY_SECONDS = 0.7
FETCH_RETRIES = 3
FETCH_RETRY_DELAY_SECONDS = 2.0

# Shot/PBP normalization batch size: how many games' worth of events get
# normalized and committed per db.begin()/advisory-lock lifetime. Small
# enough that the writer lock is released frequently -- bounding how long a
# single venue can starve a waiting Desk tick to roughly one batch's
# duration instead of the whole venue's (the 87.7-minute production
# incident this module's docstring and
# docs/plans/summer-league-cron-desk-starvation-spec.md exist to prevent) --
# while staying large enough that per-batch transaction/lock-acquisition
# overhead doesn't dominate on a routine, mostly-already-normalized run.
EVENT_BATCH_SIZE = 8

# Identity-resolution write-batch size: how many prepared resolution plans
# get applied and committed per db.begin()/advisory-lock lifetime. Mirrors
# EVENT_BATCH_SIZE's reasoning -- the candidate-search/Gemini calls that used
# to run inside this same lock happen up front in `_run_resolution_phase`'s
# preparation pass, so this batch size only governs how long the lock is
# held for the (fast, DB-only) writes.
RESOLUTION_BATCH_SIZE = 8


class SummerLeagueScheduleLockUnavailable(RuntimeError):
    """Raised internally when schedule persistence cannot acquire the writer lock."""


def _log_identity_duplicate_audit(report: IdentityDuplicateAuditReport) -> None:
    """Emit the recurring duplicate audit's summary and reviewable groups."""
    logger.info(
        "player_identity_duplicate_audit groups=%d likely_duplicates=%d",
        len(report.groups),
        report.likely_duplicate_count,
    )
    for group in report.groups:
        log = (
            logger.info
            if group.classification == "identified_namesakes"
            else logger.warning
        )
        log(
            "player_identity_duplicate_group normalized_name=%r "
            "classification=%s players=%s",
            group.normalized_name,
            group.classification,
            [(member.player_id, member.display_name) for member in group.members],
        )


def _telemetry_step(telemetry: PipelineTelemetry | None, name: str, **fields: object):
    """Return a timing context only when a production telemetry run is active.

    ``fields`` are forwarded to :meth:`PipelineTelemetry.step` unchanged (a
    no-op when ``telemetry`` is ``None``, matching that case's existing
    ``nullcontext()`` behavior). The yielded value is the same as
    ``telemetry.step``'s own: the mutable extra-fields dict when telemetry is
    active, or ``None`` from ``nullcontext()`` otherwise -- callers that want
    to add fields discovered mid-step (e.g. ``writer_lock_wait_ms``,
    ``games_processed``) must guard on ``is not None`` before writing to it.
    """
    return telemetry.step(name, **fields) if telemetry is not None else nullcontext()


def _resolve_year() -> int:
    """Resolve the Summer League year, defaulting to the current season.

    Raises:
        ValueError: If ``SL_INGEST_YEAR`` is set but is not a plausible
            four-digit season year. Failing here (rather than deep inside a
            per-venue fetch, where ``normalize_season`` would raise and get
            swallowed as "no games") makes a misconfigured schedule fail
            loudly with a non-zero exit code instead of silently ingesting
            nothing for every venue.
    """
    raw = os.getenv("SL_INGEST_YEAR")
    if not raw or not raw.strip():
        return DEFAULT_YEAR
    stripped = raw.strip()
    try:
        year = int(stripped)
    except ValueError as exc:
        raise ValueError(
            f"SL_INGEST_YEAR must be a four-digit year, got {stripped!r}"
        ) from exc
    if not 1900 <= year <= 2100:
        raise ValueError(
            f"SL_INGEST_YEAR must be a four-digit year in [1900, 2100], got {year}"
        )
    return year


def _resolve_league_ids() -> list[str]:
    """Resolve the venue LeagueIDs to ingest from env, defaulting to all three."""
    raw = os.getenv("SL_INGEST_LEAGUE_IDS")
    values = raw.split(",") if raw and raw.strip() else list(DEFAULT_LEAGUE_IDS)

    league_ids: list[str] = []
    seen: set[str] = set()
    for value in values:
        stripped = value.strip()
        if not stripped:
            continue
        league_id = normalize_league_id(stripped)
        if league_id in seen:
            continue
        seen.add(league_id)
        league_ids.append(league_id)

    if not league_ids:
        raise ValueError("No Summer League LeagueIDs resolved for ingestion")
    return league_ids


_TRUTHY_ENV_VALUES = frozenset({"1", "true", "yes", "on"})


def _full_reconcile_requested() -> bool:
    """Whether ``SL_INGEST_FULL_RECONCILE`` requests a full batch-progress reprocess.

    See the module docstring's env-var documentation. Absent/blank/falsy
    (``"0"``, ``"false"``, ``"no"``, ``"off"``) all resolve to ``False`` --
    the ordinary dirty-detection-only path.
    """
    raw = os.getenv("SL_INGEST_FULL_RECONCILE")
    if not raw or not raw.strip():
        return False
    return raw.strip().lower() in _TRUTHY_ENV_VALUES


# Outer lifecycle phases worth an NBA Stats schedule-feed round-trip: the
# event is on the calendar (Announced), imminent (Warm-up), literally
# playing (Active), or in its short wind-down tail (a day or two past the
# last known game, per `post_roll_days` -- games can still slip/reschedule
# in that window). Dormant (nowhere near the window) and Archived (long
# over) are excluded so this hourly cron never calls stats.nba.com during
# the ~11 off-season months. Wider than `app/cli/sl_desk_tick.py`'s own
# `_BOOTSTRAP_ELIGIBLE_PHASES` (which omits Wind-down) because that bootstrap
# only exists to escape dormancy -- once awake, the Desk tick's own step 0
# keeps polling every tick regardless of phase. This cron has no such
# always-runs-when-awake step of its own, so it must keep covering Wind-down
# itself to catch a late score/reschedule correction after the last game.
_SCHEDULE_ELIGIBLE_PHASES = frozenset(
    {
        EventLifecyclePhase.ANNOUNCED,
        EventLifecyclePhase.WARMUP,
        EventLifecyclePhase.ACTIVE,
        EventLifecyclePhase.WINDDOWN,
    }
)


def _synthetic_schedule_dates(
    competitions: Sequence[SummerLeagueEdition],
) -> tuple[date, ...]:
    """Every day spanned by each target competition's ``starts_on``/``ends_on``.

    Mirrors ``app/cli/sl_desk_tick.py``'s ``_synthetic_calendar_dates`` --
    duplicated here (not imported) since that module is a CLI entrypoint
    script, not a shared service, and this runner is itself a separate CLI
    entrypoint; both independently reuse the same two columns
    ``normalization.refresh_competition_date_window`` populates from real
    game data. Used as a stand-in for a real per-day ``summer_league_games``
    calendar so :func:`~app.services.event_desk.lifecycle.lifecycle_phase`'s
    gap-bridge clustering has something to reason about even on a run that
    hasn't ingested any games yet itself.

    Args:
        competitions: The target competitions for today
            (:func:`~app.services.summer_league.scoreboard_ingest.resolve_target_competitions`).

    Returns:
        Every date in each competition's inclusive ``[starts_on, ends_on]``
        span, possibly empty (and possibly containing duplicates across
        competitions -- `lifecycle_phase`'s clustering dedupes internally).
        A competition missing either date contributes nothing.
    """
    dates: list[date] = []
    for competition in competitions:
        if competition.starts_on is None or competition.ends_on is None:
            continue
        span_days = (competition.ends_on - competition.starts_on).days
        if span_days < 0:
            continue
        dates.extend(
            competition.starts_on + timedelta(days=offset)
            for offset in range(span_days + 1)
        )
    return tuple(dates)


async def _schedule_pull_in_window(db: AsyncSession, *, now: datetime) -> bool:
    """Window guard: is a registered Summer League competition in/near its window?

    Reuses the same pure outer-lifecycle state machine
    (:func:`~app.services.event_desk.lifecycle.lifecycle_phase`) the Summer
    League Desk itself uses, rather than hand-rolling date-range arithmetic,
    so this cron's notion of "in season" never drifts from the Desk's. Fed
    by each target competition's ``starts_on``/``ends_on`` window spread
    into a synthetic per-day calendar (:func:`_synthetic_schedule_dates`) --
    mirrors ``app/cli/sl_desk_tick.py``'s ``_needs_scoreboard_bootstrap`` /
    `_synthetic_calendar_dates`` pattern for the identical "no
    ``summer_league_games`` rows yet to anchor the resolver" gap, since this
    guard runs *before* any schedule ingest this cron might do -- there may
    be no real per-day game dates yet to read.

    Uses :data:`~app.services.event_desk.registry.SUMMER_LEAGUE_REGISTRATION`'s
    static ``window_priors`` default rather than reading them back off the
    persisted ``events`` row (the only value ever written there, via
    ``EventRegistration.sync``) -- deliberately so this guard stays a pure
    read with no ``events`` upsert side effect just to decide there's
    nothing to do.

    A competition with zero games ever ingested *and* no
    ``starts_on``/``ends_on`` configured (a true first-ever cold start,
    before this cron -- or anything else -- has ever recorded a game date
    for it) has no signal to reason about and this returns ``False``. That
    mirrors the exact trade-off ``_needs_scoreboard_bootstrap`` already
    makes for the Desk tick: a from-scratch season's first game date has to
    get onto ``summer_league_games`` some other way (e.g. an operator's
    manual scoreboard-ingest run, or this cron's own per-venue raw
    ``leaguegamelog`` fetch once real games start appearing) before either
    guard can self-trigger the forward-schedule call.

    Args:
        db: Active database session.
        now: The run's reference instant (used both for the Eastern "today"
            competition-year fallback and as `lifecycle_phase`'s clock).

    Returns:
        Whether the caller should run :func:`~app.services.summer_league.scoreboard_ingest.run_scoreboard_ingest`
        this cycle.
    """
    today = to_eastern_date(now)
    competitions = await resolve_target_competitions(db, today=today)
    if not competitions:
        return False
    synthetic_dates = _synthetic_schedule_dates(competitions)
    if not synthetic_dates:
        return False
    desk_event = DeskEvent(
        key=SUMMER_LEAGUE_REGISTRATION.key,
        priority=SUMMER_LEAGUE_REGISTRATION.priority,
        window_priors=SUMMER_LEAGUE_REGISTRATION.window_priors,
        game_dates=synthetic_dates,
    )
    return lifecycle_phase(now, desk_event) in _SCHEDULE_ELIGIBLE_PHASES


async def _refresh_schedule(
    db: AsyncSession, *, now: datetime, client: NBAStatsClient
) -> ScoreboardIngestReport | None:
    """Step 5 -- refresh the active event's forward schedule, if in/near its window.

    Best-effort and additive: any failure (including an unexpected error
    inside :func:`_schedule_pull_in_window` itself) is logged and swallowed
    rather than failing the whole cron run -- this step exists to keep
    ``tip_datetime`` fresh for the (separate) Summer League Desk tick to
    read, not to gate this runner's own raw-ingestion/backbone/metrics
    responsibilities.

    Args:
        db: Active database session (caller controls the transaction).
        now: The run's reference instant.
        client: The NBA Stats client already opened by ``main`` for this
            run's per-venue raw fetches -- reused here rather than opening a
            second client.

    Returns:
        The :class:`~app.services.summer_league.scoreboard_ingest.ScoreboardIngestReport`
        when the window guard allowed a fetch; ``None`` when skipped
        (off-window) or on an unexpected failure.
    """
    try:
        in_window = await _schedule_pull_in_window(db, now=now)
        # `_schedule_pull_in_window`'s own read (`resolve_target_competitions`)
        # auto-begins a transaction on this session (SQLAlchemy async
        # "autobegin"); commit it here -- a no-op for the DB since nothing
        # was written -- before opening the explicit `db.begin()` below,
        # which would otherwise raise "a transaction is already begun".
        await db.commit()
        if not in_window:
            logger.info("Schedule refresh: skipped (off-window)")
            return None

        async def _acquire_before_upsert() -> None:
            if not await try_acquire_summer_league_writer_lock(db):
                raise SummerLeagueScheduleLockUnavailable

        report = await run_scoreboard_ingest(
            db,
            today=to_eastern_date(now),
            client=client,
            before_fetch=db.commit,
            before_upsert=_acquire_before_upsert,
        )
        await db.commit()
        logger.info(
            "Schedule refresh: competitions_checked=%d games_seen=%d "
            "created=%d updated=%d errors=%s unresolved_team_ids=%s",
            report.competitions_checked,
            report.games_seen,
            report.games_created,
            report.games_updated,
            report.errors,
            report.unresolved_team_ids,
        )
        return report
    except SummerLeagueScheduleLockUnavailable:
        logger.info("Schedule refresh: skipped (Desk writer is active)")
        await db.rollback()
        return None
    except Exception as exc:
        logger.warning(
            "Schedule refresh failed (%s: %s); continuing", type(exc).__name__, exc
        )
        # Leave the session usable for whatever `main` does next (the
        # metrics-rebuild step's own `db.begin()`): a mid-query failure can
        # leave an autobegun transaction needing a rollback, and `db.begin()`
        # raises (rather than no-ops) if one is still open.
        await db.rollback()
        return None


def _describe_shot_report(report: SummerLeagueShotEventReport) -> str:
    """Format one shot-event batch report for logging."""
    return (
        f"shot events: {report.shot_events_upserted} upserted "
        f"({report.games_with_shots}/{report.games_processed} games with shots)"
    )


def _describe_pbp_report(report: SummerLeaguePBPEventReport) -> str:
    """Format one PBP-event batch report for logging."""
    return (
        f"PBP events: {report.pbp_events_upserted} upserted "
        f"({report.games_with_pbp}/{report.games_processed} games with PBP)"
    )


async def _run_lock_bounded_batches(
    db: AsyncSession,
    *,
    league_id: str,
    phase_label: str,
    unit_label: str,
    batches: Sequence[Sequence[Any]],
    telemetry: PipelineTelemetry | None,
    contention_reason: str,
    process_batch: Callable[[Sequence[Any], dict[str, object] | None], Awaitable[None]],
) -> bool:
    """Run pre-chunked ``batches`` one at a time, each its own lock/transaction.

    Shared lock-acquire/defer/telemetry loop behind both
    :func:`_run_batched_phase` (shot/PBP normalization) and
    :func:`_run_resolution_phase` (identity resolution): open ``db.begin()``,
    try the writer lock (yielding first to a waiting Desk tick via
    :func:`~app.services.ingest.write_lock.try_acquire_summer_league_writer_lock_yielding`),
    defer and stop on contention, otherwise run ``process_batch`` for this
    batch and move on. Releasing the lock between batches is what bounds how
    long either kind of lower-priority writer can starve the hourly Summer
    League Desk tick (the 87.7-minute production incident this module exists
    to prevent).

    Args:
        db: Async database session shared across this venue's phases.
        league_id: NBA Stats LeagueID for this venue (log/telemetry only).
        phase_label: Short label identifying this phase in telemetry step
            names and log lines (e.g. ``"shot"``, ``"pbp"``, ``"resolution"``).
        unit_label: Plural noun for the deferral log line (e.g. ``"games"``,
            ``"source players"``).
        batches: Pre-chunked work items; each inner sequence is one batch.
        telemetry: Optional structured-timing recorder for the production run.
        contention_reason: Passed to ``defer_full_reconciliation`` as
            ``reason=f"venue:{league_id}:{contention_reason}"`` when a batch
            cannot acquire the lock.
        process_batch: Awaited with ``(batch, step_fields)`` inside the open
            transaction once the lock is held; ``step_fields`` is the dict
            yielded by the enclosing ``telemetry.step(...)`` call (or
            ``None`` when no production telemetry run is active) for the
            callback to add its own fields to.

    Returns:
        True once every batch was attempted and committed (including the
        trivial case of zero batches). False if a batch could not acquire
        the writer lock -- reconciliation was deferred for it, and the
        caller should stop this venue's pipeline for the rest of this run.
    """
    for batch_index, batch in enumerate(batches, start=1):
        with _telemetry_step(
            telemetry, f"venue:{league_id}:{phase_label}_batch_{batch_index}"
        ) as step_fields:
            async with db.begin():
                if not await try_acquire_summer_league_writer_lock_yielding(
                    db, step_fields
                ):
                    await defer_full_reconciliation(
                        db, reason=f"venue:{league_id}:{contention_reason}"
                    )
                    logger.info(
                        "L%s: %s deferred at batch %d/%d because the Desk writer "
                        "is active; a later scheduled run will resume the "
                        "remaining %s",
                        league_id,
                        phase_label,
                        batch_index,
                        len(batches),
                        unit_label,
                    )
                    return False
                await process_batch(batch, step_fields)
    return True


async def _run_batched_phase(
    db: AsyncSession,
    *,
    year: int,
    league_id: str,
    phase: SummerLeagueBatchPhase,
    game_ids: Sequence[str],
    normalize: Callable[..., Awaitable[Any]],
    describe: Callable[[Any], str],
    telemetry: PipelineTelemetry | None,
    events_processed: Callable[[Any], int] | None = None,
) -> bool:
    """Normalize ``game_ids`` for one phase in small, independently committed batches.

    Reads durable per-game progress (see
    :mod:`app.services.ingest.batch_progress`) first so a run
    resumed after a crash/interruption only reprocesses games that were
    never committed for this phase -- and, as a side effect, a routine run
    against an already-fully-normalized venue only ever processes newly
    discovered games. Each batch then commits in its own
    ``db.begin()``/advisory-lock lifetime, releasing the writer lock
    between batches via
    :func:`~app.services.ingest.write_lock.try_acquire_summer_league_writer_lock_yielding`
    (which yields to a waiting Desk tick before each reacquisition), so one
    venue's shot/PBP volume can never again hold the lock for the venue's
    full duration (the 87.7-minute production incident this module exists to
    prevent).

    Args:
        db: Async database session shared across this venue's phases.
        year: Summer League season year.
        league_id: NBA Stats LeagueID for this venue.
        phase: Which batched phase this call is running.
        game_ids: The venue's full discovered game-id universe this run;
            games already durably completed for this phase are filtered
            out internally before batching.
        normalize: ``normalize_shot_events`` or ``normalize_pbp_events``.
        describe: Formats one batch's report into a log message.
        telemetry: Optional structured-timing recorder for the production run.
        events_processed: Extracts the batch's upserted-event count from its
            report (``report.shot_events_upserted`` or
            ``report.pbp_events_upserted``) for the per-batch
            ``events_processed`` telemetry field. ``None`` (the default,
            used by tests that call this function directly with
            ``telemetry=None``) skips that one field -- ``games_processed``
            is still recorded whenever the report carries it.

    Returns:
        True once every remaining batch was attempted and committed this
        run (including the trivial case of zero remaining games). False if
        a batch could not acquire the writer lock -- reconciliation was
        deferred for it, and the caller must stop this venue's pipeline for
        the rest of this run since a later scheduled run resumes from
        exactly the durable progress this call already committed.
    """
    completed_ids = await get_completed_batch_game_ids(
        db, year=year, league_id=league_id, phase=phase
    )
    await db.commit()  # close the read's autobegun transaction
    remaining = [game_id for game_id in game_ids if game_id not in completed_ids]
    if not remaining:
        logger.info(
            "L%s: %s normalization has nothing new to process (%d already complete)",
            league_id,
            phase.value,
            len(completed_ids),
        )
        return True

    batches = chunked(remaining, EVENT_BATCH_SIZE)

    async def _process_batch(
        batch: Sequence[str], step_fields: dict[str, object] | None
    ) -> None:
        report = await normalize(
            db,
            year=year,
            league_id=league_id,
            raw_root=RAW_ROOT,
            game_ids=set(batch),
        )
        await record_batch_progress(
            db,
            year=year,
            league_id=league_id,
            phase=phase,
            game_ids=batch,
        )
        if step_fields is not None:
            games_processed = getattr(report, "games_processed", None)
            if games_processed is not None:
                step_fields["games_processed"] = games_processed
            if events_processed is not None:
                step_fields["events_processed"] = events_processed(report)
        logger.info("L%s %s", league_id, describe(report))

    return await _run_lock_bounded_batches(
        db,
        league_id=league_id,
        phase_label=phase.value,
        unit_label="games",
        batches=batches,
        telemetry=telemetry,
        contention_reason=f"{phase.value}_batch_lock_contended",
        process_batch=_process_batch,
    )


async def _run_resolution_phase(
    db: AsyncSession,
    *,
    year: int,
    league_id: str,
    telemetry: PipelineTelemetry | None,
) -> tuple[bool, SummerLeagueResolutionReport]:
    """Resolve this venue's pending source players in small, lock-bounded batches.

    Splits identity resolution into a preparation pass -- no transaction or
    writer lock held, since this is where every candidate-search/Gemini call
    for the venue happens -- and a write phase, chunked into small
    independently committed batches exactly like :func:`_run_batched_phase`'s
    shot/PBP normalization. This is what bounds identity resolution's share
    of the 87.7-minute production incident this module exists to prevent:
    previously ``resolve_summer_league_players`` ran every unresolved
    player's candidate search (including Gemini calls) inside the same
    venue-wide transaction/writer lock as the rest of backbone normalization.

    Args:
        db: Async database session with no open transaction.
        year: Summer League season year.
        league_id: NBA Stats LeagueID for this venue.
        telemetry: Optional structured-timing recorder for the production run.

    Returns:
        A ``(completed_fully, report)`` tuple. ``completed_fully`` is False
        when a write batch could not acquire the writer lock -- reconciliation
        was deferred, and the caller should stop this venue's pipeline for
        the rest of this run. A later scheduled run resumes correctly with no
        extra bookkeeping, but *not* because already-resolved source players
        drop out of the load scope -- they don't. This caller always passes
        both ``year`` and ``league_id``, which routes ``_load_source_players``
        through its scoped branch (competition game-log/participation
        membership), and that branch carries no ``resolution_status`` filter
        (unlike the unscoped ``year is None and league_id is None`` branch).
        Every source player reachable in this competition is reloaded and
        re-prepared on every run, resolved or not. What actually makes a
        resolved player's re-preparation cheap is
        :func:`prepare_source_player_resolution` short-circuiting on a
        non-``None`` ``canonical_player_id`` before it ever reaches candidate
        search. Source players still pending -- ``VECTOR_CANDIDATE`` or
        ``UNRESOLVED``, with no ``canonical_player_id`` to short-circuit on --
        have no such shortcut: they re-run the full candidate-search cascade,
        including its Gemini embedding call, on every hourly invocation until
        they resolve. There is currently no status filter or re-embed
        cooldown bounding that repeated cost; see item 6 of the Phase 1
        post-merge minor-findings bundle (#719).
    """
    with _telemetry_step(telemetry, f"venue:{league_id}:resolution_preparation"):
        pairs = await prepare_summer_league_player_resolutions(
            db,
            year=year,
            league_id=league_id,
            before_candidate_search=db.commit,
        )
        pairs = [
            (
                source_player,
                await revalidate_source_player_resolution_plan(
                    db,
                    source_player,
                    plan,
                ),
            )
            for source_player, plan in pairs
        ]
    await db.commit()  # close the preparation read's autobegun transaction

    if not pairs:
        logger.info("L%s: resolution has no pending source players", league_id)
        return True, build_resolution_report(year=year, league_id=league_id, results=[])

    results: list[SummerLeagueResolutionResult] = []
    batches = chunked(pairs, RESOLUTION_BATCH_SIZE)

    async def _process_batch(
        batch: Sequence[tuple[Any, Any]], step_fields: dict[str, object] | None
    ) -> None:
        for source_player, plan in batch:
            results.append(
                await apply_source_player_resolution_plan(
                    db,
                    source_player,
                    plan,
                    create_stub=True,
                    recheck_variant_before_stub=False,
                )
            )
        if step_fields is not None:
            step_fields["source_players_processed"] = len(batch)

    completed = await _run_lock_bounded_batches(
        db,
        league_id=league_id,
        phase_label="resolution",
        unit_label="source players",
        batches=batches,
        telemetry=telemetry,
        contention_reason="resolution_batch_lock_contended",
        process_batch=_process_batch,
    )
    return completed, build_resolution_report(
        year=year, league_id=league_id, results=results
    )


async def _reconcile_batch_progress(
    db: AsyncSession,
    *,
    year: int,
    league_id: str,
    fetch_manifest: SummerLeagueRawManifest,
    full_reconcile: bool,
) -> None:
    """Invalidate stale batch-progress rows before the batched shot/PBP phases run.

    ``SummerLeagueBatchProgress`` rows are durable but not permanent (see
    that table's docstring): once written, a game is skipped on every later
    run, which is what makes a routine run against an unchanged venue cheap.
    But that same permanence means a completed game whose raw file changes
    later -- a forced re-fetch correcting a bad box score, a corrected
    PBP/shot-chart snapshot -- would otherwise be silently skipped forever.
    This closes that gap via two independent triggers, both clearing
    progress rows so :func:`_run_batched_phase`'s existing ``remaining``
    filter naturally reprocesses the affected games with no change needed
    to that filter itself:

    * Dirty-game detection (routine runs): any game whose
      ``shotchartdetail``/``playbyplayv2`` raw file was actually rewritten
      this run (see
      ``app.services.summer_league.raw_ingestion.dirty_game_ids_from_manifest``)
      has its SHOT/PBP progress marker cleared, scoped per-phase to the
      endpoint that phase actually reads -- a rewritten box-score file alone
      never invalidates SHOT/PBP progress, since neither phase reads it.
    * ``SL_INGEST_FULL_RECONCILE`` (operator escape hatch): clears every
      SHOT/PBP progress row for this venue/year outright, forcing a
      complete reprocess regardless of what changed on disk.

    This table is only ever written by this cron runner (never by the
    Summer League Desk tick), so this opens its own short transaction
    directly rather than going through the shared-writer-lock dance the
    Desk-coordinated phases below use.

    Args:
        db: Async database session with no open transaction.
        year: Summer League season year.
        league_id: NBA Stats LeagueID for this venue.
        fetch_manifest: This run's per-game fetch manifest.
        full_reconcile: Whether ``SL_INGEST_FULL_RECONCILE`` is set for this run.
    """
    async with db.begin():
        if full_reconcile:
            await invalidate_batch_progress(
                db, year=year, league_id=league_id, phase=SummerLeagueBatchPhase.SHOT
            )
            await invalidate_batch_progress(
                db, year=year, league_id=league_id, phase=SummerLeagueBatchPhase.PBP
            )
            logger.info(
                "L%s: SL_INGEST_FULL_RECONCILE set; cleared all shot/PBP batch "
                "progress for %s",
                league_id,
                year,
            )
            return

        shot_dirty = dirty_game_ids_from_manifest(
            fetch_manifest, endpoints=("shotchartdetail",)
        )
        pbp_dirty = dirty_game_ids_from_manifest(
            fetch_manifest, endpoints=("playbyplayv2",)
        )
        if shot_dirty:
            await invalidate_batch_progress(
                db,
                year=year,
                league_id=league_id,
                phase=SummerLeagueBatchPhase.SHOT,
                game_ids=shot_dirty,
            )
            logger.info(
                "L%s: invalidated SHOT batch progress for %d dirty game(s)",
                league_id,
                len(shot_dirty),
            )
        if pbp_dirty:
            await invalidate_batch_progress(
                db,
                year=year,
                league_id=league_id,
                phase=SummerLeagueBatchPhase.PBP,
                game_ids=pbp_dirty,
            )
            logger.info(
                "L%s: invalidated PBP batch progress for %d dirty game(s)",
                league_id,
                len(pbp_dirty),
            )


async def _log_batch_backlog(
    db: AsyncSession,
    *,
    year: int,
    league_id: str,
    discovered_game_ids: Sequence[str],
    telemetry: PipelineTelemetry | None,
) -> None:
    """Log the SHOT/PBP dirty-game backlog left outstanding for this venue.

    Surfaces :func:`~app.services.ingest.batch_progress.count_pending_batch_games`
    (#626) as a queryable/loggable metric (this ticket's scope item) rather
    than something an operator can only infer from raw file timestamps.
    Runs from the caller's ``finally`` block so the backlog is reported
    whether this venue's batched phases fully completed, were deferred by
    lock contention, or raised -- the deferred/failed cases are exactly when
    this number matters most.

    Best-effort: a read failure here is logged and swallowed rather than
    turning an otherwise-successful (or already-failed, independently
    reported) venue run into a failure over a metrics read.

    Args:
        db: Async database session with no open transaction.
        year: Summer League season year.
        league_id: NBA Stats LeagueID for this venue.
        discovered_game_ids: This run's full discovered game-id universe for
            the venue (``game_ids_universe`` in :func:`_run_venue`).
        telemetry: Optional structured-timing recorder for the production run.
    """
    try:
        shot_pending = await count_pending_batch_games(
            db,
            year=year,
            league_id=league_id,
            phase=SummerLeagueBatchPhase.SHOT,
            discovered_game_ids=discovered_game_ids,
        )
        await db.commit()  # close the read's autobegun transaction
        pbp_pending = await count_pending_batch_games(
            db,
            year=year,
            league_id=league_id,
            phase=SummerLeagueBatchPhase.PBP,
            discovered_game_ids=discovered_game_ids,
        )
        await db.commit()
    except Exception as exc:
        logger.warning(
            "L%s: failed to compute batch backlog (%s: %s)",
            league_id,
            type(exc).__name__,
            exc,
        )
        return

    with _telemetry_step(
        telemetry,
        f"venue:{league_id}:batch_backlog",
        shot_pending=shot_pending,
        pbp_pending=pbp_pending,
    ):
        pass
    logger.info(
        "L%s: batch backlog shot_pending=%d pbp_pending=%d",
        league_id,
        shot_pending,
        pbp_pending,
    )


async def _run_venue(
    db: AsyncSession,
    ingestor: SummerLeagueRawIngestor,
    *,
    year: int,
    league_id: str,
    telemetry: PipelineTelemetry | None = None,
    full_reconcile: bool = False,
) -> tuple[bool, bool]:
    """Incrementally fetch and process one Summer League venue.

    Backbone normalization, shot normalization, and PBP normalization each
    run in their own transaction/advisory-lock lifetime -- shot and PBP
    further split into small per-game batches via :func:`_run_batched_phase`
    -- instead of one venue-wide ``db.begin()`` block. This is what bounds
    how long this lower-priority writer can starve the hourly Summer League
    Desk tick: previously the whole block ran for as long as 87.7 minutes in
    production; now the lock is released and reacquired (yielding first to
    a waiting Desk tick, see
    :func:`~app.services.ingest.write_lock.try_acquire_summer_league_writer_lock_yielding`)
    at every phase and batch boundary.

    Before the batched shot/PBP phases run, :func:`_reconcile_batch_progress`
    invalidates any stale durable progress markers -- dirty games detected
    from this run's fetch manifest, or every marker outright under
    ``SL_INGEST_FULL_RECONCILE`` -- so a corrected raw file is reprocessed
    rather than silently skipped forever (see that function's docstring).

    Args:
        db: Async database session shared across venues this run.
        ingestor: Raw NBA Stats ingestor (shared client/store across venues).
        year: Summer League season year.
        league_id: NBA Stats LeagueID for this venue.
        telemetry: Optional structured-timing recorder for the production run.
        full_reconcile: Whether ``SL_INGEST_FULL_RECONCILE`` is set for this
            run (see :func:`_full_reconcile_requested`).

    Returns:
        A ``(had_games, failed)`` tuple. ``had_games`` is True when at least
        one game was discovered for this venue this run. ``failed`` is True
        only for a genuine processing failure that happened *after* games
        were found -- fetch-stage errors and lock-contention deferrals are
        folded into non-failure outcomes since there is nothing more to do
        this run either way.
    """
    try:
        # Step 1: refresh the season index only (limit_games=0 skips all
        # per-game downloads) so newly scheduled/played games become visible
        # without re-downloading every already-fetched game file.
        refresh_options = RawIngestionOptions(
            year=year,
            league_id=league_id,
            limit_games=0,
            force=True,
            delay_seconds=FETCH_DELAY_SECONDS,
        )
        with _telemetry_step(telemetry, f"venue:{league_id}:season_index_fetch"):
            refresh_manifest = ingestor.fetch_year_league(refresh_options)

        if not refresh_manifest.game_ids:
            logger.info("L%s: no games yet", league_id)
            return False, False

        # Step 2: fetch any newly-appeared games. Existing per-game files are
        # skipped (force=False), so only new games actually download.
        fetch_options = RawIngestionOptions(
            year=year,
            league_id=league_id,
            force=False,
            delay_seconds=FETCH_DELAY_SECONDS,
        )
        with _telemetry_step(telemetry, f"venue:{league_id}:game_fetch"):
            fetch_manifest = ingestor.fetch_year_league(fetch_options)
        logger.info(
            "L%s: %d games discovered (%d files written, %d skipped, %d errors)",
            league_id,
            fetch_manifest.game_count,
            len(fetch_manifest.files_written),
            len(fetch_manifest.files_skipped),
            len(fetch_manifest.errors),
        )
    except Exception as exc:
        logger.warning(
            "L%s: raw fetch failed (%s: %s); treating as no games yet this run",
            league_id,
            type(exc).__name__,
            exc,
        )
        return False, False

    # Phase A: backbone normalization (audit/competition/player-log rows --
    # identity resolution is its own Phase A2 below) -- its own transaction/
    # lock lifetime, separated from shot/PBP below so the writer lock is
    # released the instant it commits instead of staying held across the
    # whole venue.
    try:
        with _telemetry_step(
            telemetry, f"venue:{league_id}:backbone_normalization"
        ) as step_fields:
            async with db.begin():
                if not await try_acquire_summer_league_writer_lock_yielding(
                    db, step_fields
                ):
                    await defer_full_reconciliation(
                        db,
                        reason=f"venue:{league_id}:shared_write_phase_lock_contended",
                    )
                    logger.info(
                        "L%s: deferred DB processing because the Desk writer is active; "
                        "a later scheduled full run will reconcile it",
                        league_id,
                    )
                    return False, False
                backfill_options = SummerLeagueBackfillOptions(
                    year=year,
                    league_id=league_id,
                    raw_root=RAW_ROOT,
                    create_stubs=True,
                    include_resolution=False,
                )
                report = await backfill_summer_league_backbone(db, backfill_options)
                logger.info(
                    "L%s backbone: %s", league_id, summarize_backfill_report(report)
                )
    except Exception as exc:
        logger.error(
            "L%s backbone normalization failed: %s", league_id, exc, exc_info=True
        )
        return True, True

    # Phase A2: identity resolution -- a preparation pass (candidate search,
    # including every Gemini call for this venue) with no transaction or
    # writer lock held, then small lock-bounded write batches. See
    # `_run_resolution_phase`: the July 19, 2026 incident's root cause was
    # exactly this candidate search running while the writer lock was held.
    try:
        resolution_completed, resolution_report = await _run_resolution_phase(
            db, year=year, league_id=league_id, telemetry=telemetry
        )
        logger.info(
            "L%s resolution: total=%d resolved=%d unresolved=%d stubs=%d",
            league_id,
            resolution_report.total_source_players,
            resolution_report.resolved_source_players,
            resolution_report.unresolved_source_players,
            resolution_report.stubs_created,
        )
    except Exception as exc:
        logger.error(
            "L%s identity resolution failed: %s", league_id, exc, exc_info=True
        )
        return True, True
    if not resolution_completed:
        # Already deferred by `_run_resolution_phase`; stop here rather than
        # immediately racing the Desk again for the shot/PBP phases.
        return True, False

    # Phases B & C: shot / PBP normalization, chunked into small
    # independently committed per-game batches -- see `_run_batched_phase`.
    game_ids_universe = sorted(set(fetch_manifest.game_ids))
    try:
        await _reconcile_batch_progress(
            db,
            year=year,
            league_id=league_id,
            fetch_manifest=fetch_manifest,
            full_reconcile=full_reconcile,
        )
        shot_completed_fully = await _run_batched_phase(
            db,
            year=year,
            league_id=league_id,
            phase=SummerLeagueBatchPhase.SHOT,
            game_ids=game_ids_universe,
            normalize=normalize_shot_events,
            describe=_describe_shot_report,
            events_processed=lambda report: report.shot_events_upserted,
            telemetry=telemetry,
        )
        if not shot_completed_fully:
            # Already deferred by `_run_batched_phase`; stop here rather
            # than immediately racing the Desk again for the PBP phase.
            return True, False

        pbp_completed_fully = await _run_batched_phase(
            db,
            year=year,
            league_id=league_id,
            phase=SummerLeagueBatchPhase.PBP,
            game_ids=game_ids_universe,
            normalize=normalize_pbp_events,
            describe=_describe_pbp_report,
            events_processed=lambda report: report.pbp_events_upserted,
            telemetry=telemetry,
        )
        if not pbp_completed_fully:
            return True, False
    except Exception as exc:
        logger.error(
            "L%s shot/PBP normalization failed: %s", league_id, exc, exc_info=True
        )
        return True, True
    finally:
        # Best-effort, always runs (success, deferral, or exception): the
        # dirty-game backlog left outstanding is exactly the observability
        # gap this ticket closes -- an operator can otherwise only infer it
        # from raw file timestamps. See `_log_batch_backlog`.
        await _log_batch_backlog(
            db,
            year=year,
            league_id=league_id,
            discovered_game_ids=game_ids_universe,
            telemetry=telemetry,
        )

    competition_id = (
        report.competition_games.competition_id if report.competition_games else None
    )
    if competition_id is not None:
        await _retry_incomplete_team_boxes(
            db,
            ingestor,
            year=year,
            league_id=league_id,
            competition_id=competition_id,
            telemetry=telemetry,
        )

    return True, False


async def _retry_incomplete_team_boxes(
    db: AsyncSession,
    ingestor: SummerLeagueRawIngestor,
    *,
    year: int,
    league_id: str,
    competition_id: int,
    telemetry: PipelineTelemetry | None = None,
) -> None:
    """Force-refetch and re-normalize any game still on the team-box fallback.

    The main fetch step above uses ``force=False``, so a per-game box-score
    file fetched moments too early -- the game just finished, NBA Stats
    hasn't posted the official box yet -- is cached forever and never
    revisited. Every run, this closes that gap: any game whose team row is
    still sourced from the season-gamelog fallback (see
    :func:`~app.services.summer_league.normalization.find_incomplete_team_box_game_ids`,
    which is how such a game is recognized -- it never carries team minutes)
    gets one fresh, forced re-fetch and re-normalize pass. Bounded to a
    single retry per run: a game still incomplete after the forced re-fetch
    stays that way until the next scheduled run rather than looping here.

    Network I/O runs with no transaction open (mirrors the main fetch step),
    so a slow or blocked NBA Stats response never leaves a DB transaction
    idle. Because that releases the writer lock held by the initial backbone
    transaction, the retry transaction reacquires it before writing. It
    re-normalizes only competition/team box rows; it deliberately does not
    rerun player logs, identity resolution, embedding, shot, or PBP work. The
    normal hourly backbone pass already populated player lines from either
    per-game boxes or the complete season-gamelog fallback, so the missing input
    to the advanced-metric gate is specifically the team box.
    """
    async with db.begin():
        incomplete_ids = await find_incomplete_team_box_game_ids(
            db, competition_id=competition_id
        )
    if not incomplete_ids:
        return

    logger.info(
        "L%s: retrying %d game(s) still on the team-box fallback: %s",
        league_id,
        len(incomplete_ids),
        ", ".join(incomplete_ids),
    )
    retry_options = RawIngestionOptions(
        year=year,
        league_id=league_id,
        force=True,
        game_ids=tuple(incomplete_ids),
        skip_endpoints=("playbyplayv2", "shotchartdetail"),
        delay_seconds=FETCH_DELAY_SECONDS,
    )
    try:
        with _telemetry_step(telemetry, f"venue:{league_id}:team_box_refetch"):
            retry_manifest = ingestor.fetch_year_league(retry_options)
    except Exception as exc:
        logger.warning(
            "L%s: retry re-fetch failed (%s: %s); will retry again next run",
            league_id,
            type(exc).__name__,
            exc,
        )
        return
    if retry_manifest.errors:
        logger.warning(
            "L%s: %d error(s) re-fetching incomplete games; will retry again next run",
            league_id,
            len(retry_manifest.errors),
        )

    try:
        with _telemetry_step(
            telemetry, f"venue:{league_id}:team_box_normalization"
        ) as step_fields:
            async with db.begin():
                if not await try_acquire_summer_league_writer_lock_yielding(
                    db, step_fields
                ):
                    await defer_full_reconciliation(
                        db,
                        reason=f"venue:{league_id}:team_box_retry_lock_contended",
                    )
                    logger.info(
                        "L%s: team-box retry deferred because the Desk writer is active; "
                        "a later scheduled full run will retry it",
                        league_id,
                    )
                    return
                competition_report = await normalize_competition_games(
                    db,
                    year=year,
                    league_id=league_id,
                    raw_root=RAW_ROOT,
                )
                # The forced fetch happens after the raw-file audit, so its
                # content hash cannot affect this run's watermark. Force the
                # derivative gate instead whenever retry normalization succeeds.
                await defer_full_reconciliation(
                    db,
                    reason=f"venue:{league_id}:team_box_retry_normalized",
                )
                logger.info(
                    "L%s team-box normalization (retry pass): %d team rows",
                    league_id,
                    competition_report.team_game_logs_upserted,
                )
    except Exception as exc:
        logger.error(
            "L%s box re-normalize (retry pass) failed: %s",
            league_id,
            exc,
            exc_info=True,
        )


async def main() -> int:
    """Run the incremental Summer League ingestion cycle for all venues.

    Returns:
        Exit code (0 for success, including the pre-tip-off no-op case;
        1 for a genuine failure).
    """
    start_time = datetime.now(timezone.utc)
    client: NBAStatsClient | None = None
    telemetry = PipelineTelemetry(job="full_ingestion", logger=logger)

    try:
        try:
            year = _resolve_year()
            league_ids = _resolve_league_ids()
        except ValueError as exc:
            logger.error("Invalid Summer League ingest configuration: %s", exc)
            telemetry.finish("failed")
            return 1
        full_reconcile = _full_reconcile_requested()

        logger.info(
            "Starting Summer League ingestion: year=%s league_ids=%s full_reconcile=%s",
            year,
            ",".join(league_ids),
            full_reconcile,
        )

        failed = False
        any_games = False

        client = NBAStatsClient(
            timeout=FETCH_TIMEOUT_SECONDS,
            max_retries=FETCH_RETRIES,
            retry_delay_seconds=FETCH_RETRY_DELAY_SECONDS,
        )
        store = SummerLeagueRawStore(RAW_ROOT)
        ingestor = SummerLeagueRawIngestor(
            client=client,
            store=store,
            progress=lambda message: logger.info(message),
        )

        async with SessionLocal() as db:
            for league_id in league_ids:
                with telemetry.step(f"venue:{league_id}"):
                    had_games, venue_failed = await _run_venue(
                        db,
                        ingestor,
                        year=year,
                        league_id=league_id,
                        telemetry=telemetry,
                        full_reconcile=full_reconcile,
                    )
                any_games = any_games or had_games
                failed = failed or venue_failed

            # Step 5 -- forward-schedule refresh (decoupled from the Desk
            # tick, see module docstring). Independent of `any_games`: a
            # venue can be scoreless this run (pre-tip-off) while the
            # schedule feed still has fresh tip times to upsert. Reuses the
            # NBA Stats client already opened above; best-effort, so a
            # failure here never flips `failed`.
            with telemetry.step("schedule_refresh"):
                await _refresh_schedule(db, now=start_time, client=client)

            with telemetry.step("identity_duplicate_audit"):
                duplicate_report = await audit_variant_player_duplicates(db)
                _log_identity_duplicate_audit(duplicate_report)

            metrics_failed = await run_metrics_stage(
                db,
                context=MetricsStageContext(
                    year=year,
                    any_games=any_games,
                    upstream_succeeded=not failed,
                    telemetry=telemetry,
                    force_reconcile=full_reconcile,
                ),
                before_derivatives=db.commit,
            )
            failed = failed or metrics_failed

            if failed:
                async with db.begin():
                    await record_pipeline_failure(
                        db,
                        job=SummerLeaguePipelineJob.FULL_INGESTION,
                        reason="one or more venue or derivative stages failed",
                    )
    finally:
        if client is not None:
            client.close()
        await dispose_engine()

    elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
    telemetry.finish("failed" if failed else "succeeded")
    if failed:
        logger.error("Summer League ingestion finished with failures in %.1fs", elapsed)
        return 1

    logger.info("Summer League ingestion finished successfully in %.1fs", elapsed)
    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
