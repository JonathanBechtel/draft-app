"""Vocabulary shared by the three Summer League Desk latency classes (#699).

`docs/plans/summer-league-desk-simplification-spec.md` §2 partitions the Desk
tick along how fresh each class of work needs to be, because the hourly tick's
critical path was coupled to work with wildly different latency profiles: an
~88-minute venue ingest could starve a Desk tick that itself takes ~38
seconds, and it did so *most often while games were live* -- exactly when the
Desk is most valuable.

This module holds what all three classes need: the class vocabulary
(:class:`DeskLatencyClass`), the writer-lock policy that lets each class
declare whether it participates in backbone serialization at all
(:class:`WriterLockPolicy`), and the window/dormancy resolution every class
must perform before doing anything.

**The rule the policy encodes** (spec §2 "Rules"):

===============  ==========================================================
Class            Writer lock
===============  ==========================================================
Fast             **None.** It writes a narrow, well-scoped set of canonical
                 rows (``summer_league_games`` scores/status/tip times) and
                 must never queue behind the backbone.
Projection       **None.** A pure reader of canonical data plus a writer of
                 Desk projection tables it exclusively owns.
Backbone         **The shared lock**, bounded. It writes broad canonical
                 state (normalized logs, identities, materialized metrics)
                 and still serializes against the full ingestion runner.
===============  ==========================================================

The composite entrypoint (`app.services.summer_league.desk_tick.composite`)
passes a *lock-taking* policy to all three so the pre-#699 single-cron
behavior is preserved byte-for-byte; the standalone per-class entrypoints
pass :data:`NO_WRITER_LOCK` for fast/projection.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.event_desk import EventDailyState, EventLifecyclePhase
from app.schemas.player_affiliation import AffiliationStatus
from app.schemas.summer_league import SummerLeagueCompetition, SummerLeagueGame
from app.schemas.summer_league_desk import SummerLeagueCohortBaseline
from app.schemas.summer_league_pipeline import SummerLeaguePipelineJob
from app.services.event_desk.lifecycle import lifecycle_phase
from app.services.event_desk.registry import (
    SUMMER_LEAGUE_REGISTRATION,
    DeskEvent,
    WindowPriors,
)
from app.services.event_desk.state_machine import inner_state
from app.services.event_desk.timeutils import to_eastern_date
from app.services.summer_league.nba_stats_client import NBAStatsClient
from app.services.summer_league.pipeline_telemetry import PipelineTelemetry
from app.services.summer_league.scoreboard_ingest import resolve_target_competitions
from app.services.summer_league.write_lock import (
    acquire_summer_league_writer_lock_bounded_timed,
)

DEFAULT_RAW_ROOT = Path("data/raw/nba_stats/summer_league")

# Maximum wall-clock time a lock-taking class will wait for the shared writer
# lock before giving up (#622 -- a long-running full-ingestion cron holding the
# lock previously starved the Desk for over an hour). Only the backbone class
# waits on this at all once #699's partition is deployed; the fast and
# projection classes take no lock, which is the whole point of the partition.
DEFAULT_WRITER_LOCK_MAX_WAIT_SECONDS = 30.0

# Roster statuses `desk_storylines.compute_desk_storylines` treats as "on the
# active roster tonight" (mirrored here so grading covers the same universe
# storylines will read T2 back for).
ROSTER_ACTIVE_STATUSES = frozenset(
    {
        AffiliationStatus.ANNOUNCED,
        AffiliationStatus.CONFIRMED,
        AffiliationStatus.ACTIVE,
    }
)


class DeskLatencyClass(str, Enum):
    """One independently-scheduled Summer League Desk workload (spec §2).

    The value is the ``job`` label
    :class:`~app.services.summer_league.pipeline_telemetry.PipelineTelemetry`
    stamps on every structured log line, so "the tick was slow" resolves to
    *which* class without reading the code -- ticket #699's per-class
    telemetry requirement.
    """

    FAST = "desk_fast"
    PROJECTION = "desk_projection"
    BACKBONE = "desk_backbone"
    #: The pre-#699 single orchestrator, retained so the existing cron and
    #: every caller of ``run_desk_tick`` keep working unchanged.
    COMPOSITE = "desk"

    @property
    def pipeline_job(self) -> SummerLeaguePipelineJob:
        """The ``summer_league_pipeline_states`` row this class reports into."""
        return _PIPELINE_JOB_BY_CLASS[self]


_PIPELINE_JOB_BY_CLASS: dict[DeskLatencyClass, SummerLeaguePipelineJob] = {
    DeskLatencyClass.FAST: SummerLeaguePipelineJob.DESK_FAST,
    DeskLatencyClass.PROJECTION: SummerLeaguePipelineJob.DESK_PROJECTION,
    DeskLatencyClass.BACKBONE: SummerLeaguePipelineJob.DESK_BACKBONE,
    DeskLatencyClass.COMPOSITE: SummerLeaguePipelineJob.DESK,
}


@dataclass(frozen=True)
class WriterLockPolicy:
    """Whether a latency class participates in shared writer-lock serialization.

    Spec §2 requires the fast path to take **no global lock** and the
    projection path to need none either. Rather than scattering
    ``if take_lock:`` through three runners, each runner is handed a policy
    and calls :meth:`acquire` unconditionally; a disabled policy is a no-op
    that still records why it did nothing.

    Attributes:
        enabled: Whether :meth:`acquire` actually takes the shared lock.
        max_wait_seconds: Bounded wait forwarded to
            :func:`~app.services.summer_league.write_lock.acquire_summer_league_writer_lock_bounded_timed`.
        telemetry: Optional run timer; each acquire is recorded as its own
            step so a contended backbone run is visible in logs.
    """

    enabled: bool = True
    max_wait_seconds: float = DEFAULT_WRITER_LOCK_MAX_WAIT_SECONDS
    telemetry: Optional[PipelineTelemetry] = None

    async def acquire(
        self, db: AsyncSession, *, step: str = "writer_lock_wait"
    ) -> None:
        """Take the shared writer lock, or do nothing when this class is lock-free.

        Args:
            db: Active database session (caller controls the transaction --
                the lock is transaction-scoped and released on commit/rollback).
            step: Telemetry step name, so an initial acquire and a
                post-provider-I/O reacquisition stay distinguishable in logs.

        Raises:
            SummerLeagueWriterLockTimeout: The lock was not obtained within
                ``max_wait_seconds``. Never raised by a disabled policy --
                which is precisely why the fast class cannot be starved.
        """
        if not self.enabled:
            return
        with (
            self.telemetry.step(step) if self.telemetry is not None else nullcontext()
        ) as step_fields:
            await acquire_summer_league_writer_lock_bounded_timed(
                db,
                max_wait_seconds=self.max_wait_seconds,
                step_fields=step_fields,
            )


#: The policy the lock-free classes use. Reads at the call site as an
#: assertion about the class, not an accident of defaulting.
NO_WRITER_LOCK = WriterLockPolicy(enabled=False)


@dataclass(frozen=True)
class TickContext:
    """Everything a latency-class runner needs that is *not* about its own work.

    All three classes were originally written taking the same seven keyword
    arguments -- clock, wall-clock start, raw root, provider client,
    transaction policy, telemetry, lock policy -- which made every signature
    wide enough to trip the complexity ratchet and, worse, made it easy for the
    composite to hand one class a slightly different set than another. Bundling
    them means the composite constructs the run's context **once** and every
    class provably runs under the same one.

    Attributes:
        now: The resolved reference instant (naive UTC), after any
            ``SL_DESK_FORCE_DATE`` override.
        executed_at: The real wall-clock instant the run began, or ``None``
            for "same as ``now``". It differs from ``now`` only under that
            override, and is kept separate because operational telemetry must
            report when the job actually ran, never the pretend clock. Read it
            through :attr:`started_at`, which resolves the ``None`` case.
        raw_root: Root directory of audited raw Summer League snapshots.
        client: Optional injected NBA Stats client (tests, and the composite
            sharing one session across steps 0 and 1).
        transaction_boundary: Caller-supplied hook that ends the current
            database transaction, invoked before provider requests so network
            latency never leaves one open. ``None`` (the default) means the
            caller keeps a single transaction across the whole run, which is
            the legacy test/service contract.

            This is a **callback rather than a boolean on purpose.** These
            modules live under ``app/services/`` but are scheduled-job
            orchestration, not request-bounded code; the repo forbids services
            from calling ``commit()``/``rollback()`` themselves
            (``scripts/check_request_transaction_policy.py``). Inverting the
            dependency keeps that rule intact at full strength: the classes
            declare *where* a transaction may be released, and only the
            ``app/cli/`` entrypoints decide *how*.
        session_configurator: Optional hook for session-scoped database
            settings that a latency class must reapply after a transaction
            boundary. The standalone fast entrypoint uses this for bounded
            PostgreSQL lock and statement timeouts; it is deliberately absent
            from the composite so the rollback path keeps its legacy session
            behavior.
        telemetry: Optional run timer.
        lock: Writer-lock policy. Defaults to :data:`NO_WRITER_LOCK` -- the
            fast and projection classes' defining property.
    """

    now: datetime
    executed_at: Optional[datetime] = None
    raw_root: Path = DEFAULT_RAW_ROOT
    client: Optional[NBAStatsClient] = None
    transaction_boundary: Optional[Callable[[], Awaitable[None]]] = None
    session_configurator: Optional[Callable[[AsyncSession], Awaitable[None]]] = None
    telemetry: Optional[PipelineTelemetry] = None
    lock: WriterLockPolicy = NO_WRITER_LOCK

    @property
    def releases_transactions(self) -> bool:
        """Whether this run ends its transaction around provider I/O."""
        return self.transaction_boundary is not None

    async def release_transaction(self) -> None:
        """End the current transaction, if this run has a boundary at all."""
        if self.transaction_boundary is not None:
            await self.transaction_boundary()

    @property
    def started_at(self) -> datetime:
        """When the run actually began, falling back to :attr:`now`."""
        return self.executed_at if self.executed_at is not None else self.now


async def resolve_daily_state(
    db: AsyncSession, *, now: datetime
) -> Optional[EventDailyState]:
    """Cheap pre-check: is the SL event's inner daily state resolvable right now?

    Mirrors the per-registration resolution
    ``app.services.event_desk.controller.run_event_desk_tick`` performs
    internally (that helper is private to the controller module) so a class
    can decide, *before* touching the network or any T2/T3/T4 table, whether
    it's off-window and therefore inert. Wind-down is content-active even
    though the inner state machine only accepts Active events, so this
    pre-check maps Wind-down directly to Recap, matching the request path.
    ``registration.sync`` is the same idempotent ``events`` row upsert the
    controller's own first step performs -- the only "write" this pre-check
    does.

    Args:
        db: Active database session.
        now: The tick's reference instant (naive UTC).

    Returns:
        The resolved daily state, or ``None`` outside the Active/Wind-down
        content window.
    """
    registration = SUMMER_LEAGUE_REGISTRATION
    event_row = await registration.sync(db, now.date())
    calendar_facts = await registration.provider.resolve_calendar_facts(db, now=now)
    desk_event = DeskEvent(
        key=registration.key,
        priority=event_row.priority,
        window_priors=WindowPriors.from_dict(event_row.window_priors),
        game_dates=calendar_facts.game_dates,
    )
    if lifecycle_phase(now, desk_event) == EventLifecyclePhase.WINDDOWN:
        return EventDailyState.RECAP
    return inner_state(
        now, calendar_facts.today_schedule, calendar_facts.today_statuses, desk_event
    )


# Outer lifecycle phases #527's bootstrap should attempt for: the event is on
# the calendar (Announced), imminent (Warm-up), or literally its first known
# day (Active) -- as opposed to Dormant (nowhere near the window) or Archived
# (long over), which must stay network-free per #516's cost decision.
_BOOTSTRAP_ELIGIBLE_PHASES = frozenset(
    {
        EventLifecyclePhase.ANNOUNCED,
        EventLifecyclePhase.WARMUP,
        EventLifecyclePhase.ACTIVE,
    }
)


def synthetic_calendar_dates(
    competitions: Sequence[SummerLeagueCompetition],
) -> tuple[date, ...]:
    """Every day spanned by each competition's configured ``starts_on``/``ends_on``.

    Used only as a stand-in for real ``summer_league_games`` dates when a
    competition genuinely has zero game rows yet -- `lifecycle_phase`'s
    gap-bridge clustering (`app.services.event_desk.lifecycle`) sees an empty
    calendar as "far off," which is the #527 chicken-and-egg bug: no games
    yet means no anchor to even try the schedule feed that would create them.
    A competition missing either date contributes nothing (there's no
    fallback anchor to synthesize for it).

    Args:
        competitions: The target competitions for today (`resolve_target_competitions`).

    Returns:
        Every date in each competition's inclusive ``[starts_on, ends_on]``
        span, possibly empty (and possibly containing duplicates across
        competitions -- `lifecycle_phase`'s clustering dedupes internally).
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


async def needs_scoreboard_bootstrap(db: AsyncSession, *, now: datetime) -> bool:
    """#527 -- should this off-window tick still attempt scoreboard ingest?

    :func:`resolve_daily_state` resolves ``None`` (inert) both for a genuinely
    dormant event *and* for an event whose window has arrived but has zero
    ``summer_league_games`` rows yet to anchor `lifecycle_phase`'s gap-bridge
    clustering (the very first morning of the season, before the scoreboard
    step has ever run) -- a chicken-and-egg gap, since that step is exactly
    what would create the anchor. This helper distinguishes the two: a real
    game already exists (the normal resolver is authoritative -- no bootstrap
    needed), or a configured ``starts_on``/``ends_on`` places the event's
    *outer* lifecycle phase in Announced/Warm-up/Active
    (:data:`_BOOTSTRAP_ELIGIBLE_PHASES`) -- only then does this return
    ``True``. A competition with no configured dates either, or one whose
    synthetic phase is Dormant/Wind-down/Archived, returns ``False`` and the
    caller stays network-free (preserves #516's deliberate off-window cost
    decision).

    Since #699 this is the **fast** class's concern: scoreboard ingest is
    fast-class work, so bootstrap belongs there and the projection/backbone
    classes simply stay inert until an anchor exists.

    Args:
        db: Active database session (caller controls the transaction).
        now: The tick's reference instant (naive UTC).

    Returns:
        Whether the caller should run ``run_scoreboard_ingest`` before
        re-attempting :func:`resolve_daily_state`.
    """
    today = to_eastern_date(now)
    competitions = await resolve_target_competitions(db, today=today)
    if not competitions:
        return False

    competition_ids = [c.id for c in competitions if c.id is not None]
    if competition_ids:
        has_games_stmt = (
            select(SummerLeagueGame.id)  # type: ignore[call-overload]
            .where(SummerLeagueGame.competition_id.in_(competition_ids))  # type: ignore[attr-defined]
            .limit(1)
        )
        if (await db.execute(has_games_stmt)).first() is not None:
            # A real anchor already exists somewhere in this year's
            # competitions; the normal resolver is authoritative.
            return False

    dates = synthetic_calendar_dates(competitions)
    if not dates:
        return False

    registration = SUMMER_LEAGUE_REGISTRATION
    event_row = await registration.sync(db, today)
    synthetic_event = DeskEvent(
        key=registration.key,
        priority=event_row.priority,
        window_priors=WindowPriors.from_dict(event_row.window_priors),
        game_dates=dates,
    )
    return lifecycle_phase(now, synthetic_event) in _BOOTSTRAP_ELIGIBLE_PHASES


async def active_baseline_version(db: AsyncSession) -> Optional[str]:
    """The currently active T1 ``baseline_version``, or ``None`` if Job A hasn't run."""
    stmt = (
        select(SummerLeagueCohortBaseline.baseline_version)  # type: ignore[call-overload]
        .where(SummerLeagueCohortBaseline.is_active.is_(True))  # type: ignore[attr-defined]
        .limit(1)
    )
    row = (await db.execute(stmt)).first()
    return row[0] if row else None


def require_baseline_version(baseline_version: Optional[str]) -> str:
    """Assert Job A has run, raising the long-standing operator-facing message.

    Args:
        baseline_version: The result of :func:`active_baseline_version`.

    Returns:
        The baseline version, narrowed to ``str``.

    Raises:
        RuntimeError: No active T1 cohort baseline exists.
    """
    if baseline_version is None:
        raise RuntimeError(
            "No active Summer League cohort baseline (T1) found -- run "
            "scripts/build_sl_cohort_baselines.py before the desk tick."
        )
    return baseline_version
