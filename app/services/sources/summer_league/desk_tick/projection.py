"""The **medium** Summer League Desk latency class -- the projection builder (#699).

`docs/plans/summer-league-desk-simplification-spec.md` §2:

===============  ===========================================================
Cadence          ~Hourly, or on input change
Latency budget   < 1 min
Touches          Reads canonical, writes the Desk projection
Writer lock      **None**
===============  ===========================================================

Five steps, all of them reads of canonical data plus writes to Desk
projection tables this class exclusively owns:

  3. **grades (T2)** -- bulk-graded against the active T1 baseline (#503/#548,
     ``desk_grades.grade_players_bulk``): ONE batched pass for the whole
     roster, not a per-player loop.
  4. **storylines (T3 + T4)** -- ``desk_storylines.compute_desk_storylines``
     for today's games.
  5. **commentary** (#524) -- all eight #520 detectors per graded player, each
     fed by a batched peer-population fetch, persisted onto T2 and (grouped by
     tonight's rosters) onto each touched T4 slate row. See
     :mod:`app.services.sources.summer_league.desk_tick.commentary`.
  6. **render/state freshness** -- upsert ``event_desk_state`` (#506
     ``event_desk.controller.run_event_desk_tick``, the only module that
     writes that table).
  7. **render snapshot materialization** (#551) -- build the COMPLETE
     Preview/Live/Recap x Tracker cohort/stat-view variant matrix and
     atomically upsert every row in ONE bounded statement. This is what lets
     the homepage read a single persisted snapshot at request time instead of
     reassembling the Desk on every visit.

**Why this class takes no lock** (spec §2): "The medium path is a *pure
reader* of canonical data plus a writer of its own projection. It should not
need the backbone writer lock at all -- its only writes are to Desk projection
tables it exclusively owns." Nothing here writes ``summer_league_games``,
normalized logs, identities, or materialized metrics; those belong to the fast
and backbone classes respectively.

**Never rebuilds a distribution.** Job A (``scripts/build_sl_cohort_baselines.py``)
is the rare, offline cohort-baseline (T1) builder; this class only ever reads
the currently active baseline version and fails loudly if none exists.

**Steps 6 and 7 are all-or-nothing with steps 3-5.** They run inside the
caller's transaction, so a failure anywhere rolls this run's writes back
wholesale and whatever the prior successful run materialized is never
overwritten with a partial result.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.event_desk import EventDailyState, EventDeskState
from app.schemas.summer_league import SummerLeagueEdition
from app.services.event_desk.controller import run_event_desk_tick
from app.services.event_desk.render_snapshots import (
    RenderSnapshotWrite,
    upsert_render_snapshots,
)
from app.services.event_desk.timeutils import to_eastern_date
from app.services.sources.summer_league.desk_grades import GradeRow, grade_players_bulk
from app.services.sources.summer_league.desk_read import build_desk_render_variants
from app.services.sources.summer_league.desk_storylines import (
    StorylineTickResult,
    compute_desk_storylines,
)
from app.services.sources.summer_league.desk_tick.commentary import (
    active_roster_player_ids,
    commentary_for_competition,
)
from app.services.sources.summer_league.desk_tick.shared import (
    TickContext,
    active_baseline_version,
    require_baseline_version,
    resolve_daily_state,
)
from app.services.sources.summer_league.scoreboard_ingest import (
    resolve_target_competitions,
)


@dataclass(frozen=True)
class ProjectionTickResult:
    """Outcome of one :func:`run_projection_tick` call."""

    now: datetime
    executed_at: datetime
    dormant: bool
    daily_state: Optional[EventDailyState]
    baseline_version: Optional[str] = None
    graded_player_ids: tuple[int, ...] = ()
    storyline_results: dict[int, StorylineTickResult] = field(default_factory=dict)
    event_desk_states: tuple[EventDeskState, ...] = ()
    #: Variant rows upserted -- 0 off-window, otherwise the full matrix.
    materialized_variant_count: int = 0

    @property
    def content_updated(self) -> bool:
        """Whether this run actually rebuilt Desk content."""
        return not self.dormant


async def materialize_render_snapshots(db: AsyncSession, *, now: datetime) -> int:
    """Step 7 (#551) -- build and atomically upsert the render-variant matrix.

    ``source_freshness_tick_at``/``source_freshness_next_tick_eta`` are read
    off each variant's own freshly-assembled ``DeskFreshness`` rather than
    re-queried -- every variant in one run shares the identical freshness
    stamp by construction (``build_desk_render_variants`` resolves it once and
    reuses it across the whole matrix). Spec §3's corollary: the render
    snapshots remain a legitimate cache, but they must stamp the watermark of
    the projection they render. Keep that discipline.

    Args:
        db: Active database session (caller controls the transaction; never
            commits here).
        now: The run's reference instant (naive UTC).

    Returns:
        The number of variant rows upserted -- 0 when the event is off-window
        (nothing to materialize; any prior snapshots are left untouched,
        never truncated).
    """
    result = await build_desk_render_variants(db, now=now, now_is_effective=True)
    if result is None:
        return 0
    event_id, variants = result

    writes = [
        RenderSnapshotWrite(
            event_id=event_id,
            daily_state=variant.daily_state,
            tracker_cohort=variant.tracker_cohort,
            tracker_stat_view=variant.tracker_stat_view,
            view=variant.view,
            source_freshness_tick_at=(
                variant.view.payload.freshness.last_tick_at
                if variant.view.payload is not None
                else None
            ),
            source_freshness_next_tick_eta=(
                variant.view.payload.freshness.next_tick_eta
                if variant.view.payload is not None
                else None
            ),
        )
        for variant in variants
    ]
    await upsert_render_snapshots(db, writes, now=now)
    return len(writes)


async def run_projection_tick(
    db: AsyncSession,
    ctx: TickContext,
    *,
    competitions: Optional[tuple[SummerLeagueEdition, ...]] = None,
    daily_state: Optional[EventDailyState] = None,
) -> ProjectionTickResult:
    """Rebuild the Desk projection from canonical data (spec §2, medium class).

    Args:
        db: Active database session (caller controls the transaction).
        ctx: The run's shared context. Its ``lock`` defaults to
            :data:`NO_WRITER_LOCK` per spec §2; the composite entrypoint
            supplies a lock-taking policy to preserve pre-#699 behavior.
        competitions: Pre-resolved target competitions, so the composite can
            resolve them once and share across classes.
        daily_state: Pre-resolved daily state, for the same reason. When
            omitted this class resolves it itself.

    Returns:
        A :class:`ProjectionTickResult`.

    Raises:
        RuntimeError: There is real work to do but no active T1 cohort
            baseline exists -- Job A must run first.
    """
    now = ctx.now
    telemetry = ctx.telemetry
    # Unconditional: the shared lock is a re-entrant transaction-scoped
    # advisory lock, so the composite (which already holds it) pays nothing
    # here, and a standalone run gets the acquire it needs. Cheaper than a
    # special-case flag that only the composite ever sets.
    await ctx.lock.acquire(db)
    started_at = ctx.started_at

    if daily_state is None:
        daily_state = await resolve_daily_state(db, now=now)
    if daily_state is None:
        with (
            telemetry.step(
                "dormant_noop",
                executed_at=started_at.isoformat(),
                effective_data_at=now.isoformat(),
                content_updated=False,
            )
            if telemetry is not None
            else nullcontext()
        ):
            pass
        return ProjectionTickResult(
            now=now, executed_at=started_at, dormant=True, daily_state=None
        )

    baseline_version = require_baseline_version(await active_baseline_version(db))
    today = to_eastern_date(now)

    if competitions is None:
        with (
            telemetry.step("resolve_target_competitions")
            if telemetry is not None
            else nullcontext()
        ):
            competitions = tuple(await resolve_target_competitions(db, today=today))

    mode: Literal["morning", "live"] = (
        "morning" if daily_state == EventDailyState.PREVIEW else "live"
    )

    graded_player_ids: list[int] = []
    storyline_results: dict[int, StorylineTickResult] = {}

    with telemetry.step("desk_projections") if telemetry is not None else nullcontext():
        for competition in competitions:
            assert competition.id is not None
            competition_id = competition.id

            # Step 3 -- grades (T2). Players with no data/baseline are
            # silently omitted (see `grade_players_bulk`'s docstring), the
            # same skip semantics the old per-player try/except performed.
            roster_player_ids = await active_roster_player_ids(db, competition_id)
            grade_by_player: dict[int, GradeRow] = await grade_players_bulk(
                db, roster_player_ids, competition_id, baseline_version=baseline_version
            )
            graded_player_ids.extend(grade_by_player.keys())

            # Step 4 -- storylines (T3 + T4).
            result = await compute_desk_storylines(
                db,
                game_date=today,
                competition_id=competition_id,
                baseline_version=baseline_version,
                mode=mode,
            )
            storyline_results[competition_id] = result

            # Step 5 -- commentary (all eight #520 Facts onto T2 + grouped onto T4).
            await commentary_for_competition(
                db,
                competition=competition,
                baseline_version=baseline_version,
                game_date=today,
                grade_by_player=grade_by_player,
                slate=result.slate,
            )

    # Step 6 -- render/state freshness: `event_desk_state` upsert, only
    # reached once every required step above has genuinely succeeded.
    with telemetry.step("event_desk_state") if telemetry is not None else nullcontext():
        states = await run_event_desk_tick(db, now=now, content_updated=True)

    # Step 7 -- render snapshot materialization (#551). The FINAL step.
    with (
        telemetry.step("snapshot_materialization")
        if telemetry is not None
        else nullcontext()
    ):
        materialized_variant_count = await materialize_render_snapshots(db, now=now)

    return ProjectionTickResult(
        now=now,
        executed_at=started_at,
        dormant=False,
        daily_state=daily_state,
        baseline_version=baseline_version,
        graded_player_ids=tuple(graded_player_ids),
        storyline_results=storyline_results,
        event_desk_states=tuple(states),
        materialized_variant_count=materialized_variant_count,
    )
