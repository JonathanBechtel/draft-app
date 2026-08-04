"""The **fast** Summer League Desk latency class -- the live poller (#699).

`docs/plans/summer-league-desk-simplification-spec.md` §2:

===============  ===========================================================
Cadence          Minutes
Latency budget   Seconds
Touches          A narrow set of live game rows
Writer lock      **None**
===============  ===========================================================

Two steps, in this order:

  0. **schedule/scoreboard ingest** (#515, #529) -- upsert the active event's
     full known schedule (``tip_datetime`` / status / scores). The state
     machine and Morning Card cannot exist without tip times, and every later
     class reads this as the provider-authoritative live status for each game.
     Writes exactly one table: ``summer_league_games`` (plus the owning
     competition's ``starts_on``/``ends_on`` window).
  1. **targeted live raw refresh** (#531,
     :func:`~app.services.sources.summer_league.live_ingestion.run_live_ingestion`) --
     force a fresh boxscore/pbp/shotchart pull for exactly the
     Scheduled/In-Progress games in an active time window around "now", never
     the whole season. Runs *after* scoreboard so the selection reads each
     game's freshest known status. Writes **no database rows at all** -- its
     job ends the moment raw JSON is on disk.

**Why this class takes no lock.** The failure this partition descends from is
an ~88-minute venue ingest holding the shared writer lock while a ~38-second
Desk tick timed out and skipped its interval -- and it happened most often
during live games, because that is when ingestion has the most to process.
Bounded waits (#622) stopped the Desk from *hanging* but did not make it
*land*. This class's entire write surface is the narrow, provider-authoritative
game row set above, which the backbone's monotonic status resolution
(#530, ``normalization.resolve_game_status``) is already built to interleave
with, so serializing it against the backbone bought nothing and cost the
Desk its most user-visible promise.

**Identity resolution stays out-of-band** (spec §2): nothing here resolves a
player identity or blocks on one. A game whose players are not yet resolved
still gets its score and status polled; the projection class degrades
gracefully for the unresolved remainder.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.event_desk import EventDailyState
from app.services.event_desk.timeutils import to_eastern_date
from app.services.sources.summer_league.desk_tick.shared import (
    TickContext,
    needs_scoreboard_bootstrap,
    resolve_daily_state,
)
from app.services.sources.summer_league.live_ingestion import (
    LiveIngestionReport,
    run_live_ingestion,
)
from app.services.sources.summer_league.nba_stats_client import NBAStatsClient
from app.services.sources.summer_league.raw_store import SummerLeagueRawStore
from app.services.sources.summer_league.scoreboard_ingest import (
    ScoreboardIngestReport,
    run_scoreboard_ingest,
)


@dataclass(frozen=True)
class WindowResolution:
    """Whether the event is in a content window, and what it cost to find out.

    Resolving the window is the one piece of work every latency class needs
    before it can decide whether it has anything to do. The composite
    entrypoint resolves it **once** and hands it to all three classes rather
    than paying for three independent resolutions -- both to keep the
    composite's query count where it was pre-#699 and so all three classes
    provably agree about what state the event is in.

    Attributes:
        daily_state: The resolved inner daily state, or ``None`` off-window.
        bootstrap_report: The #527 pre-anchor scoreboard ingest's report when
            that path ran, else ``None``. When present the fast class must
            **not** re-issue step 0's network round-trip.
    """

    daily_state: Optional[EventDailyState]
    bootstrap_report: Optional[ScoreboardIngestReport] = None


async def resolve_window(
    db: AsyncSession,
    ctx: TickContext,
    *,
    before_upsert: Optional[Callable[[], Awaitable[None]]] = None,
) -> WindowResolution:
    """Resolve the event's content window, bootstrapping an anchor if needed.

    #527: the very first morning of the season (or any tick in the
    announce/pre-roll window) has zero ``summer_league_games`` rows to anchor
    the resolver, so it comes back dormant even though scoreboard ingest is
    exactly what would create that anchor. This attempts the bootstrap ingest
    once and re-resolves before reporting dormancy; a genuinely off-window
    tick never reaches the network (#516).

    Args:
        db: Active database session.
        ctx: The run's shared context (clock, client, transaction policy,
            telemetry).
        before_upsert: Write boundary invoked after the bootstrap's provider
            response and before its persistence.

    Returns:
        A :class:`WindowResolution`.
    """
    daily_state = await resolve_daily_state(db, now=ctx.now)
    if daily_state is not None:
        return WindowResolution(daily_state=daily_state)
    if not await needs_scoreboard_bootstrap(db, now=ctx.now):
        return WindowResolution(daily_state=None)

    with (
        ctx.telemetry.step("bootstrap_scoreboard_ingest")
        if ctx.telemetry is not None
        else nullcontext()
    ):
        bootstrap_report = await run_scoreboard_ingest(
            db,
            today=to_eastern_date(ctx.now),
            client=ctx.client,
            before_fetch=ctx.transaction_boundary,
            before_upsert=before_upsert,
        )
    return WindowResolution(
        daily_state=await resolve_daily_state(db, now=ctx.now),
        bootstrap_report=bootstrap_report,
    )


@dataclass(frozen=True)
class FastTickResult:
    """Outcome of one :func:`run_fast_tick` call."""

    now: datetime
    executed_at: datetime
    dormant: bool
    daily_state: Optional[EventDailyState]
    #: ``None`` only on a dormant tick that never reached step 0.
    scoreboard_report: Optional[ScoreboardIngestReport] = None
    #: ``None`` on a dormant tick; otherwise always populated, including the
    #: common empty-window case (``selected=0``).
    live_refresh_report: Optional[LiveIngestionReport] = None
    #: Whether the #527 pre-anchor bootstrap ran scoreboard ingest before the
    #: normal daily-state resolution succeeded.
    bootstrapped: bool = False

    @property
    def source_refreshed(self) -> bool:
        """Whether the provider answered without error this tick."""
        return self.scoreboard_report is not None and not self.scoreboard_report.errors

    @property
    def source_advanced(self) -> bool:
        """Whether this tick actually moved canonical source data forward."""
        scoreboard = self.scoreboard_report
        live = self.live_refresh_report
        return bool(
            (
                scoreboard is not None
                and (scoreboard.games_created or scoreboard.games_updated)
            )
            or (live is not None and live.written)
        )


async def run_fast_tick(
    db: AsyncSession,
    ctx: TickContext,
    *,
    window: Optional[WindowResolution] = None,
) -> FastTickResult:
    """Poll live scores/box data for in-window games (spec §2, fast class).

    Never grades, storylines, normalizes, rebuilds a metric, or writes a Desk
    projection -- those belong to the projection and backbone classes and are
    exactly the latency this class exists not to inherit.

    Args:
        db: Active database session (caller controls the transaction).
        ctx: The run's shared context. Its ``lock`` defaults to
            :data:`NO_WRITER_LOCK`, which is the entire point of this class;
            the composite entrypoint supplies a lock-taking policy to preserve
            pre-#699 behavior.
        window: A window already resolved by the caller (:func:`resolve_window`),
            so the composite pays for one resolution across all three classes
            instead of three. Resolved here when omitted.

    Returns:
        A :class:`FastTickResult`.

    Raises:
        RuntimeError: The targeted live raw refresh reported an error for a
            game it actually selected this tick (#530). Callers must not let
            a failed refresh claim fresh state.
    """
    now = ctx.now
    lock = ctx.lock
    telemetry = ctx.telemetry

    if ctx.session_configurator is not None:
        await ctx.session_configurator(db)

    await lock.acquire(db)
    started_at = ctx.started_at

    async def reacquire_writer_lock() -> None:
        """Start a short serialized write phase after external provider I/O.

        A no-op under :data:`NO_WRITER_LOCK` -- the fast class's defining
        property is that nothing here can queue behind the backbone.
        """
        if ctx.session_configurator is not None:
            await ctx.session_configurator(db)
        await lock.acquire(db, step="writer_lock_reacquire")

    before_upsert = reacquire_writer_lock if ctx.releases_transactions else None

    if window is None:
        window = await resolve_window(db, ctx, before_upsert=before_upsert)
    daily_state = window.daily_state
    bootstrap_report = window.bootstrap_report

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
        return FastTickResult(
            now=now,
            executed_at=started_at,
            dormant=True,
            daily_state=None,
            bootstrapped=bootstrap_report is not None,
        )

    # The transaction-scoped advisory lock (when a lock-taking policy is in
    # play) is intentionally released before provider I/O. Keeping it -- and a
    # database transaction -- open while NBA Stats retries can take minutes
    # trips the production idle-in-transaction guard and leaves no healthy
    # connection for the later write phase.
    await ctx.release_transaction()

    # Steps 0-1 share one NBA Stats client/session -- opened here (once) when
    # the caller didn't inject one, and always closed afterward.
    owns_client = ctx.client is None
    active_client = ctx.client or NBAStatsClient()
    try:
        # Step 0 -- schedule/scoreboard ingest. Already ran above if this tick
        # needed the #527 bootstrap; skip the duplicate network round-trip.
        if bootstrap_report is not None:
            scoreboard_report = bootstrap_report
        else:
            with (
                telemetry.step("scoreboard_ingest")
                if telemetry is not None
                else nullcontext()
            ):
                scoreboard_report = await run_scoreboard_ingest(
                    db,
                    today=to_eastern_date(now),
                    client=active_client,
                    before_fetch=ctx.transaction_boundary,
                    before_upsert=before_upsert,
                )

        # Step 1 -- targeted live raw refresh (#531/#530). Reads
        # `summer_league_games` fresh (including anything step 0 just flushed
        # this tick), so a Scheduled game bootstrapped moments ago is already
        # visible here; selection is status/window-scoped.
        with (
            telemetry.step("live_raw_refresh")
            if telemetry is not None
            else nullcontext()
        ):
            live_refresh_report = await run_live_ingestion(
                db,
                client=active_client,
                store=SummerLeagueRawStore(ctx.raw_root),
                clock=lambda: now,
                before_refresh=ctx.transaction_boundary,
            )
        if live_refresh_report.required_errors > 0:
            # A group's *required* season gamelog fetch failed outright for
            # games this tick actually selected, or a critical per-game
            # box-score endpoint failed and left the stale on-disk snapshot in
            # place. Either way normalize would silently re-read stale data as
            # fresh, so raise before anything can claim freshness (#530). A
            # merely optional per-game/endpoint hiccup (reflected in `.errors`
            # but not `.required_errors`) does not abort -- normalize already
            # tolerates partial per-game raw data.
            raise RuntimeError(
                "Required Summer League live raw refresh failed "
                f"({live_refresh_report.required_errors} required error(s)): "
                f"{'; '.join(live_refresh_report.error_messages)}"
            )
    finally:
        if owns_client:
            active_client.close()

    # A lock-taking caller (the composite) released the lock before provider
    # I/O above; hand the transaction back in the state it expects.
    if ctx.releases_transactions:
        await reacquire_writer_lock()

    return FastTickResult(
        now=now,
        executed_at=started_at,
        dormant=False,
        daily_state=daily_state,
        scoreboard_report=scoreboard_report,
        live_refresh_report=live_refresh_report,
        bootstrapped=bootstrap_report is not None,
    )
