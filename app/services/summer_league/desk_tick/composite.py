"""The pre-#699 single-orchestrator Desk tick, now assembled from three classes.

This is ``run_desk_tick`` -- unchanged in behavior, signature, and result
shape from the version that lived in `app/cli/sl_desk_tick.py`. It exists for
two reasons after the #699 latency-class partition:

1. **Migration safety.** The production ``summer-league-desk-cron`` machine
   runs this until the per-class machines are created and proven. Nothing
   about deploying the partition requires a flag day.
2. **It is the equivalence oracle.** The partition is only trustworthy if the
   three classes, run in order under one transaction, still produce exactly
   what the single orchestrator produced. Keeping the composite as a real
   caller of the same three runners is what makes that testable rather than
   asserted.

**It deliberately keeps the old locking behavior.** Every class here is handed
a lock-*taking* policy, so the composite still holds the shared writer lock
across its whole run exactly as before. That is the property #699 removes --
but only for the independently scheduled per-class entrypoints, so a rollback
to this composite is a genuine rollback and not a half-state.

Order (the behavior spec's Job B, §10):

    window resolve (+#527 bootstrap) -> baseline check -> [fast: 0, 1]
    -> [backbone: 2, 2b] -> [projection: 3, 4, 5, 6, 7]
"""

from __future__ import annotations

import logging
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.event_desk import EventDailyState, EventDeskState
from app.services.event_desk.timeutils import to_eastern_date
from app.services.summer_league.desk_read import _effective_now
from app.services.summer_league.desk_storylines import StorylineTickResult
from app.services.summer_league.desk_tick.backbone import run_backbone_tick
from app.services.summer_league.desk_tick.fast import (
    WindowResolution,
    resolve_window,
    run_fast_tick,
)
from app.services.summer_league.desk_tick.projection import run_projection_tick
from app.services.summer_league.desk_tick.shared import (
    DEFAULT_RAW_ROOT,
    DEFAULT_WRITER_LOCK_MAX_WAIT_SECONDS,
    TickContext,
    WriterLockPolicy,
    active_baseline_version,
    require_baseline_version,
)
from app.services.summer_league.live_ingestion import LiveIngestionReport
from app.services.summer_league.nba_stats_client import NBAStatsClient
from app.services.summer_league.pipeline_telemetry import PipelineTelemetry
from app.services.summer_league.scoreboard_ingest import (
    ScoreboardIngestReport,
    resolve_target_competitions,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeskTickResult:
    """Summary of one :func:`run_desk_tick` call -- every stage's outcome."""

    now: datetime
    executed_at: datetime
    dormant: bool
    daily_state: Optional[EventDailyState]
    content_updated: bool
    source_refreshed: bool = False
    source_advanced: bool = False
    baseline_version: Optional[str] = None
    scoreboard_report: Optional[ScoreboardIngestReport] = None
    # Targeted live raw refresh (#531/#530). `None` on a dormant tick;
    # otherwise always populated, including the common empty-window case
    # (`selected=0`).
    live_refresh_report: Optional[LiveIngestionReport] = None
    normalized_competition_ids: tuple[int, ...] = ()
    graded_player_ids: tuple[int, ...] = ()
    storyline_results: dict[int, StorylineTickResult] = field(default_factory=dict)
    event_desk_states: tuple[EventDeskState, ...] = ()
    # Whether the #527 pre-anchor bootstrap ran `run_scoreboard_ingest` before
    # the normal daily-state resolution succeeded.
    bootstrapped: bool = False
    # Render snapshot variant rows upserted this tick -- 0 off-window,
    # otherwise `len(DESK_RENDER_DAILY_STATES) * len(TRACKER_COHORTS) *
    # len(TRACKER_STAT_VIEWS)` (72 today).
    materialized_variant_count: int = 0


async def run_desk_tick(
    db: AsyncSession,
    *,
    now: Optional[datetime] = None,
    raw_root: Path = DEFAULT_RAW_ROOT,
    client: Optional[NBAStatsClient] = None,
    release_transactions_for_network_io: bool = False,
    telemetry: PipelineTelemetry | None = None,
    writer_lock_max_wait_seconds: float = DEFAULT_WRITER_LOCK_MAX_WAIT_SECONDS,
) -> DeskTickResult:
    """Job B -- the Summer League Desk hourly tick (module docstring has the order).

    Does not commit by default; the caller controls the transaction. The
    production cron passes ``release_transactions_for_network_io=True`` so
    provider fetches never run while an idle database transaction is open.

    Args:
        db: Active database session (caller controls the transaction).
        now: Override for "now" (tests only); defaults to the current UTC
            instant.
        raw_root: Root directory of audited raw Summer League snapshots,
            forwarded to the normalize and targeted-live-refresh steps.
        client: Optional injected :class:`NBAStatsClient` (tests only),
            forwarded to the scoreboard ingest and targeted live-refresh steps
            (they share one client/session); when omitted a real client is
            opened for the duration of those steps and closed afterward.
        release_transactions_for_network_io: Commit completed read/write work
            before provider requests, then reacquire the transaction-scoped
            writer lock before normalized/projection writes. Required for
            long-running cron execution and opt-in to preserve the legacy
            test/service caller transaction contract.
        telemetry: Optional production-run timer that emits one structured
            duration record for every major pipeline stage.
        writer_lock_max_wait_seconds: Maximum wall-clock time to wait for the
            shared writer lock -- both the initial acquire and each
            post-provider-I/O reacquisition -- before giving up (#622).

    Returns:
        A :class:`DeskTickResult` summarizing every stage's outcome.

    Raises:
        RuntimeError: The tick is not off-window (there's real work to do) but
            no active T1 cohort baseline exists -- Job A
            (``scripts/build_sl_cohort_baselines.py``) must run first; or the
            targeted live raw refresh reported an error for a game it actually
            selected this tick (#530).
        SummerLeagueWriterLockTimeout: The writer lock was not obtained within
            ``writer_lock_max_wait_seconds`` -- a long-running lower-priority
            writer (typically full ingestion) is holding it. A
            retry-next-scheduled-run condition, not a data-quality failure
            (#622). **This is the failure #699's per-class entrypoints exist
            to stop costing the Desk its live surface**: under the partition,
            only the backbone class can raise it.
    """
    # Serialize before either scheduled writer touches shared Summer League
    # identities/projections. Bounded (#622): this tick must never block past
    # an explicit maximum no matter how long a lower-priority writer holds it.
    lock = WriterLockPolicy(
        enabled=True,
        max_wait_seconds=writer_lock_max_wait_seconds,
        telemetry=telemetry,
    )
    await lock.acquire(db)

    async def reacquire_writer_lock() -> None:
        """Start a short serialized write phase after external provider I/O."""
        await lock.acquire(db, step="writer_lock_reacquire")

    # A contended lock can delay the tick across a tip/final boundary. Read
    # the production clock only after the wait so phase and freshness
    # calculations describe the instant this transaction can actually begin
    # its work. Tests keep their explicit deterministic override unchanged.
    executed_at = now if now is not None else datetime.utcnow()
    resolved_now = _effective_now(executed_at, scheduled_write=True)

    # One context for the whole run, handed to all three classes, so they
    # provably share a clock, a client, a transaction policy, and -- here --
    # the lock-taking policy that makes the composite behave exactly as it did
    # before #699.
    ctx = TickContext(
        now=resolved_now,
        executed_at=executed_at,
        raw_root=raw_root,
        client=client,
        transaction_boundary=(
            db.commit if release_transactions_for_network_io else None
        ),
        telemetry=telemetry,
        lock=lock,
    )

    window: WindowResolution = await resolve_window(
        db,
        ctx,
        before_upsert=(
            reacquire_writer_lock if release_transactions_for_network_io else None
        ),
    )

    if window.daily_state is None:
        # Off-window/dormant: content-state inert. The registration pre-check
        # may sync the canonical event row, but lifecycle/content state and
        # render snapshots are left exactly as they were. Scheduler success is
        # recorded separately by the CLI's pipeline-state projection.
        with (
            telemetry.step(
                "dormant_noop",
                executed_at=executed_at.isoformat(),
                effective_data_at=resolved_now.isoformat(),
                content_updated=False,
            )
            if telemetry is not None
            else nullcontext()
        ):
            pass
        return DeskTickResult(
            now=resolved_now,
            executed_at=executed_at,
            dormant=True,
            daily_state=None,
            content_updated=False,
            event_desk_states=(),
            bootstrapped=window.bootstrap_report is not None,
            materialized_variant_count=0,
        )

    # Checked before any provider I/O so a tick that could never produce
    # content fails immediately rather than after minutes of network work.
    baseline_version = require_baseline_version(await active_baseline_version(db))

    # Fast class -- steps 0 and 1. Handed the lock-taking policy so the
    # composite's serialization is byte-for-byte what it was pre-#699.
    fast_result = await run_fast_tick(db, ctx, window=window)

    with (
        telemetry.step("resolve_target_competitions")
        if telemetry is not None
        else nullcontext()
    ):
        competitions = tuple(
            await resolve_target_competitions(db, today=to_eastern_date(resolved_now))
        )

    # Backbone class -- steps 2 and 2b. `acquire_lock=False`: already held.
    backbone_result = await run_backbone_tick(
        db, ctx, competitions=competitions, acquire_lock=False
    )

    # Projection class -- steps 3 through 7.
    projection_result = await run_projection_tick(
        db,
        ctx,
        competitions=competitions,
        daily_state=window.daily_state,
        acquire_lock=False,
    )

    return DeskTickResult(
        now=resolved_now,
        executed_at=executed_at,
        dormant=False,
        daily_state=window.daily_state,
        content_updated=True,
        source_refreshed=fast_result.source_refreshed,
        source_advanced=(
            backbone_result.source_advanced or fast_result.source_advanced
        ),
        baseline_version=baseline_version,
        scoreboard_report=fast_result.scoreboard_report,
        live_refresh_report=fast_result.live_refresh_report,
        normalized_competition_ids=backbone_result.normalized_competition_ids,
        graded_player_ids=projection_result.graded_player_ids,
        storyline_results=projection_result.storyline_results,
        event_desk_states=projection_result.event_desk_states,
        bootstrapped=fast_result.bootstrapped,
        materialized_variant_count=projection_result.materialized_variant_count,
    )
