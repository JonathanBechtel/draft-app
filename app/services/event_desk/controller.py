"""EventDesk controller — evaluate registered events, pick the home owner, upsert state.

`docs/plans/event-desk-framework.md` ("EventDesk controller (each tick)"):

1. For every registered event, compute its lifecycle phase from calendar + game
   status.
2. Collect home-eligible events (phase in {Warm-up, Active, Wind-down}).
3. Home owner = highest `priority` among home-eligible (single owner).
4. Upsert `event_desk_state` per event with its phase/daily-state/home-ownership.

V1 registers Summer League only (`registry.REGISTERED_EVENTS`), so step 3 is
unopposed — but this module iterates the registry generically, the seam the
framework doc calls out for event #2. This is the only I/O-performing module in the
package; `lifecycle.py` and `state_machine.py` stay pure and are called from here.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.event_desk import (
    Event,
    EventDailyState,
    EventDeskState,
    EventLifecyclePhase,
)
from app.services.event_desk.lifecycle import lifecycle_phase, resolve_home_owner
from app.services.event_desk.registry import (
    REGISTERED_EVENTS,
    CalendarFacts,
    DeskEvent,
    EventRegistration,
    WindowPriors,
)
from app.services.event_desk.state_machine import inner_state

# Matches the existing hourly Fly cron (behavior spec §2 "Refresh").
TICK_INTERVAL = timedelta(hours=1)


async def _upsert_event_desk_state(
    db: AsyncSession,
    *,
    event_id: int,
    now: datetime,
    phase: EventLifecyclePhase,
    daily_state: Optional[EventDailyState],
    is_home_owner: bool,
) -> EventDeskState:
    """Idempotently upsert one event's `event_desk_state` row (keyed by `event_id`)."""
    values = {
        "event_id": event_id,
        "as_of": now,
        "lifecycle_phase": phase,
        "daily_state": daily_state,
        "is_home_owner": is_home_owner,
        # Hero content selection (storyline/grade-driven) is a later ticket's job
        # (#504 storyline engine, #508 desk read service) — this controller only
        # resolves phase/state/ownership.
        "hero_ref": None,
        "freshness_tick_at": now,
        "next_tick_eta": now + TICK_INTERVAL,
    }
    stmt = insert(EventDeskState).values(**values)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_event_desk_state_event",
        set_={k: v for k, v in values.items() if k != "event_id"},
    )
    await db.execute(stmt)
    await db.flush()

    result = await db.execute(
        select(EventDeskState).where(EventDeskState.event_id == event_id)  # type: ignore[arg-type]
    )
    return result.scalar_one()


async def _resolve_desk_event(
    db: AsyncSession,
    registration: EventRegistration,
    event_row: Event,
    *,
    now: datetime,
) -> tuple[DeskEvent, CalendarFacts]:
    """Build one tick's pure :class:`DeskEvent` + its resolved :class:`CalendarFacts`."""
    calendar_facts = await registration.provider.resolve_calendar_facts(db, now=now)
    desk_event = DeskEvent(
        key=registration.key,
        priority=event_row.priority,
        window_priors=WindowPriors.from_dict(event_row.window_priors),
        game_dates=calendar_facts.game_dates,
    )
    return desk_event, calendar_facts


async def run_event_desk_tick(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    registrations: Sequence[EventRegistration] = REGISTERED_EVENTS,
) -> list[EventDeskState]:
    """Evaluate every registered event and upsert its `event_desk_state` row.

    For each registration: (1) sync its `events` row for `now`'s date, (2) resolve
    this tick's calendar facts via its content provider, (3) compute lifecycle phase
    + (if Active) inner daily state, (4) determine home ownership via
    `lifecycle.resolve_home_owner` across every registered event, (5) upsert
    `event_desk_state`. Does not commit; the caller controls the transaction
    (mirrors every other Summer League ingest/tick step in this repo).

    Args:
        db: Active database session (caller controls the transaction/commit).
        now: Override for "now" (tests only); defaults to the current UTC instant.
        registrations: Override for the registered-events list (tests only);
            defaults to :data:`~app.services.event_desk.registry.REGISTERED_EVENTS`
            (Summer League only in V1).

    Returns:
        One upserted :class:`~app.schemas.event_desk.EventDeskState` row per
        registration, in registration order.
    """
    resolved_now = now if now is not None else datetime.utcnow()

    event_rows: list[Event] = []
    desk_events: list[DeskEvent] = []
    calendar_facts_by_key: dict[str, CalendarFacts] = {}
    for registration in registrations:
        event_row = await registration.sync(db, resolved_now.date())
        desk_event, calendar_facts = await _resolve_desk_event(
            db, registration, event_row, now=resolved_now
        )
        event_rows.append(event_row)
        desk_events.append(desk_event)
        calendar_facts_by_key[registration.key] = calendar_facts

    owner = resolve_home_owner(resolved_now, desk_events)

    states: list[EventDeskState] = []
    for registration, event_row, desk_event in zip(
        registrations, event_rows, desk_events
    ):
        phase = lifecycle_phase(resolved_now, desk_event)
        daily_state = None
        if phase == EventLifecyclePhase.ACTIVE:
            facts = calendar_facts_by_key[registration.key]
            daily_state = inner_state(
                resolved_now, facts.today_schedule, facts.today_statuses, desk_event
            )
        assert event_row.id is not None
        state = await _upsert_event_desk_state(
            db,
            event_id=event_row.id,
            now=resolved_now,
            phase=phase,
            daily_state=daily_state,
            is_home_owner=owner is not None and owner.key == desk_event.key,
        )
        states.append(state)
    return states
