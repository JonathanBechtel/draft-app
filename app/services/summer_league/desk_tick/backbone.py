"""The **slow** Summer League Desk latency class -- the backbone (#699).

`docs/plans/summer-league-desk-simplification-spec.md` §2:

===============  ===========================================================
Cadence          Hours / off-peak
Latency budget   Unbounded
Touches          Broad canonical state + materialized metrics
Writer lock      **The shared bounded lock** -- this is the class that keeps it
===============  ===========================================================

Two steps:

  2. **normalize** -- ``normalize_competition_games`` /
     ``normalize_player_game_logs`` pick up any newly audited raw box scores
     for today's competitions. Best-effort per competition: a competition
     with no audited raw run yet is not an error, since raw fetch/audit runs
     on its own cadence. Status resolution here is pure and one-way (#530,
     ``normalization.resolve_game_status``) -- a partial mid-game snapshot
     can never promote a Scheduled/In-Progress game to Final on its own, and
     a proven Final is monotonic against any later, less-complete call. That
     monotonicity is what makes it safe for the lock-free fast class to be
     writing the same ``summer_league_games`` rows concurrently.
  2b. **scoped metrics rebuild** (#523) -- refresh
     ``summer_league_player_seasons`` for exactly the competitions normalize
     touched, so the projection class reads freshly recomputed event
     aggregates. Never the unscoped whole-table rebuild
     ``scripts/rebuild_sl_metrics.py`` performs.

**Its cost must be invisible to the other two classes** (spec §2). That is
now structural rather than aspirational: the fast and projection classes take
no lock, so however long this class runs, it cannot starve them. What it
*can* still do is be expensive on its own terms -- see #701, which is that
the rebuild does a full-pool ``compute()`` on every in-event tick because the
gate only short-circuits when the input watermark is unchanged. This
partition contains that cost's blast radius; it does not remove the cost, and
#701 remains open and separate.

**Identity resolution stays out-of-band** (spec §2). It is not invoked here;
it runs in the full ingestion runner, which serializes against this class via
the same shared lock.
"""

from __future__ import annotations

import logging
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.event_desk import EventDailyState
from app.schemas.summer_league import SummerLeagueCompetition
from app.services.event_desk.timeutils import to_eastern_date
from app.services.summer_league.desk_tick.shared import (
    TickContext,
    resolve_daily_state,
)
from app.services.summer_league.metrics import rebuild as rebuild_sl_metrics
from app.services.summer_league.normalization import (
    normalize_competition_games,
    normalize_player_game_logs,
)
from app.services.summer_league.scoreboard_ingest import resolve_target_competitions

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BackboneTickResult:
    """Outcome of one :func:`run_backbone_tick` call."""

    now: datetime
    executed_at: datetime
    dormant: bool
    daily_state: Optional[EventDailyState]
    #: Competitions normalize actually touched this run, in resolution order.
    normalized_competition_ids: tuple[int, ...] = ()
    #: Whether the scoped metrics rebuild was invoked at all.
    metrics_rebuilt: bool = False

    @property
    def source_advanced(self) -> bool:
        """Whether this run moved canonical normalized data forward."""
        return bool(self.normalized_competition_ids)


async def normalize_competition(
    db: AsyncSession, competition: SummerLeagueCompetition, *, raw_root: Path
) -> bool:
    """Best-effort normalize one competition's audited raw data.

    A competition with no audited raw run yet is the common case (raw
    fetch/audit runs on its own cadence, independent of this class) -- not an
    error. ``normalize_competition_games`` raises ``ValueError`` when no
    ``summer_league_raw_runs`` row exists yet for ``(year, league_id)``, and
    file-backed parsing raises ``FileNotFoundError`` when the raw snapshot
    files aren't on disk. Both are caught here and treated as "nothing new to
    normalize" rather than aborting the whole run.

    Args:
        db: Active database session.
        competition: The competition to normalize.
        raw_root: Root directory of audited raw Summer League snapshots.

    Returns:
        Whether normalization actually ran.
    """
    try:
        await normalize_competition_games(
            db,
            year=competition.year,
            league_id=competition.league_id,
            raw_root=raw_root,
        )
        await normalize_player_game_logs(
            db,
            year=competition.year,
            league_id=competition.league_id,
            raw_root=raw_root,
        )
    except (ValueError, FileNotFoundError) as exc:
        logger.info(
            "sl_desk_backbone: skip normalize for %s/%s (%s)",
            competition.year,
            competition.league_id,
            exc,
        )
        return False
    return True


async def run_backbone_tick(
    db: AsyncSession,
    ctx: TickContext,
    *,
    competitions: Optional[tuple[SummerLeagueCompetition, ...]] = None,
    daily_state: Optional[EventDailyState] = None,
) -> BackboneTickResult:
    """Normalize audited raw data and rebuild the metrics it invalidated.

    Args:
        db: Active database session (caller controls the transaction).
        ctx: The run's shared context. Unlike the other two classes this one
            is normally given a lock-*taking* ``lock`` -- it is the class that
            keeps the shared writer lock.
        competitions: Pre-resolved target competitions, so the composite
            entrypoint can resolve them once and share them with the
            projection class rather than issuing the query twice.
        daily_state: Pre-resolved daily state, for the same reason -- the
            composite resolves the window once and shares it across all three
            classes. Resolved here when omitted.

    Returns:
        A :class:`BackboneTickResult`.

    Raises:
        SummerLeagueWriterLockTimeout: The shared writer lock was not obtained
            within the policy's bound -- a long-running lower-priority writer
            is holding it. A retry-next-scheduled-run condition, and since
            #699 one that no longer costs the Desk its live surface: the fast
            and projection classes are unaffected.
    """
    now = ctx.now
    telemetry = ctx.telemetry
    # Unconditional: the shared lock is a re-entrant transaction-scoped
    # advisory lock, so the composite (which already holds it) pays nothing
    # here, and a standalone run gets the acquire it needs.
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
        return BackboneTickResult(
            now=now, executed_at=started_at, dormant=True, daily_state=None
        )

    if competitions is None:
        with (
            telemetry.step("resolve_target_competitions")
            if telemetry is not None
            else nullcontext()
        ):
            competitions = tuple(
                await resolve_target_competitions(db, today=to_eastern_date(now))
            )

    # Step 2 -- normalize (best-effort per competition).
    normalized_ids: list[int] = []
    with telemetry.step("normalization") if telemetry is not None else nullcontext():
        for competition in competitions:
            assert competition.id is not None
            if await normalize_competition(db, competition, raw_root=ctx.raw_root):
                normalized_ids.append(competition.id)

    # Step 2b -- scoped metrics rebuild (#523), scoped by competition_id so a
    # competition this run didn't normalize -- including rows this module
    # never wrote at all -- is never deleted or replaced. A no-op (empty
    # `normalized_ids`, the common case when raw fetch/audit hasn't produced
    # anything new) skips the call entirely rather than issuing an
    # empty-scope rebuild.
    #
    # #697 changed how this publishes: build-then-flip against
    # `DatedVersionMixin`/`is_current`, not delete-then-write. This class
    # inherits that, which is why an overlapping projection read can never
    # observe a half-published metric generation.
    metrics_rebuilt = False
    if normalized_ids:
        with (
            telemetry.step("scoped_metrics_rebuild")
            if telemetry is not None
            else nullcontext()
        ):
            await rebuild_sl_metrics(db, competition_ids=normalized_ids)
        metrics_rebuilt = True

    return BackboneTickResult(
        now=now,
        executed_at=started_at,
        dormant=False,
        daily_state=daily_state,
        normalized_competition_ids=tuple(normalized_ids),
        metrics_rebuilt=metrics_rebuilt,
    )
