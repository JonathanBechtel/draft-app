"""Inner state machine — daily coverage state, only meaningful while Active (pure).

`docs/plans/event-desk-framework.md` ("Inner — Daily Coverage"):

    Preview -> Live -> Recap

This *is* the Summer League Morning Card / Live Desk / Ledger machine
(`docs/plans/summer-league-scouts-desk-behavior-spec.md` §2), renamed event-neutral
and mapped onto the persisted `event_desk_state.daily_state` enum
(`preview`/`live`/`recap`): Morning Card -> `preview`, Live Desk -> `live`,
The Ledger -> `recap`.

Resolution contract (behavior spec §2 "Resolution & data prerequisites", framework
doc): the state is resolved **at request/tick time** by this pure function over
`(now, today's schedule, today's game statuses, event)` — `event_desk_state` is a
freshness/data cache the resolver never reads back as its own verdict. This function
takes no clock reads and does no I/O, so it's fully table-driven-testable.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional, Sequence

from app.schemas.event_desk import EventDailyState, EventLifecyclePhase
from app.services.event_desk.lifecycle import lifecycle_phase
from app.services.event_desk.registry import DeskEvent, GameStatus
from app.services.event_desk.timeutils import eastern_floor_to_utc


def _flip_time(first_tip: datetime, event: DeskEvent) -> datetime:
    """The Ledger->Morning flip instant: `max(first_tip - LEAD, MORNING_FLOOR_ET)`.

    Behavior spec §2: this is the one transition game status can't drive (nothing is
    live on either side of it), so it's schedule-relative — driven by *today's first
    tip*, not an arbitrary wall-clock time. `MORNING_FLOOR_ET` is resolved on
    `first_tip`'s Eastern calendar date (DST-safe; see `timeutils.eastern_floor_to_utc`).

    Args:
        first_tip: Today's earliest known tip time (naive UTC).
        event: The event, for its `window_priors.morning_lead_h` /
            `morning_floor_et`.

    Returns:
        The naive-UTC flip instant.
    """
    priors = event.window_priors
    lead = timedelta(hours=priors.morning_lead_h)
    floor_utc = eastern_floor_to_utc(first_tip, priors.morning_floor_et)
    return max(first_tip - lead, floor_utc)


def inner_state(
    now: datetime,
    schedule: Sequence[datetime],
    statuses: Sequence[GameStatus],
    event: DeskEvent,
) -> Optional[EventDailyState]:
    """Resolve today's inner daily state (Preview/Live/Recap) at `now`.

    Returns `None` whenever `event`'s outer lifecycle phase isn't `active` — the
    inner machine only runs during Active (framework doc "Inner — Daily Coverage
    (only during Active)"); this function re-derives the outer phase itself via
    `lifecycle_phase` rather than trusting a caller-supplied flag, so it stays a
    single source of truth for "is the inner machine even in play right now."

    Rules (behavior spec §2), checked in order:

    1. **Live always wins** — any game `in_progress` -> `live`, unconditionally.
    2. **Off-day** — no games scheduled today (`schedule` empty) -> `recap`
       (Ledger persists all day; the flip never fires because there's no first tip
       to be relative to).
    3. **Today's last final** — every known game today is `final` -> `recap`
       (Ledger persists into the evening/overnight).
    4. **Scheduled-tip fallback** — `now >= today's first tip` and not every game is
       `final` -> `live`, even if no game is yet *marked* `in_progress` (a stale
       tick shouldn't make the page claim "Morning" while games are actually
       underway).
    5. **The flip** — `now >= max(first_tip - LEAD, MORNING_FLOOR_ET)` -> `preview`
       (Morning Card); otherwise `recap` (last night's Ledger still shows, pre-flip).

    Args:
        now: The tick/request instant (naive UTC).
        schedule: Naive-UTC tip times for every game on today's (Eastern-date)
            slate. Empty on an off-day.
        statuses: Every known game status for today's slate, in the event-agnostic
            `GameStatus` vocabulary (need not be pairwise-aligned with `schedule` —
            a game missing a tip time still contributes its status).
        event: The event (for its lifecycle calendar + window priors).

    Returns:
        `EventDailyState.PREVIEW` / `LIVE` / `RECAP`, or `None` when the event isn't
        currently in its Active lifecycle phase.
    """
    if lifecycle_phase(now, event) != EventLifecyclePhase.ACTIVE:
        return None

    if any(status == GameStatus.IN_PROGRESS for status in statuses):
        return EventDailyState.LIVE

    if not schedule:
        return EventDailyState.RECAP

    all_final = bool(statuses) and all(
        status == GameStatus.FINAL for status in statuses
    )
    if all_final:
        return EventDailyState.RECAP

    first_tip = min(schedule)
    if now >= first_tip:
        return EventDailyState.LIVE

    if now >= _flip_time(first_tip, event):
        return EventDailyState.PREVIEW

    return EventDailyState.RECAP
