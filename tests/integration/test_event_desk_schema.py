"""Integration tests for the generic Event Desk registry/state tables.

Schema-roundtrip coverage only — the EventDesk controller that upserts these rows
(ticket #506) is out of scope for this ticket.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.event_desk import (
    Event,
    EventCalendarSource,
    EventDailyState,
    EventDeskState,
    EventLifecyclePhase,
    EventType,
)

_N = {"i": 0}


def _make_event(**overrides: object) -> Event:
    _N["i"] += 1
    idx = _N["i"]
    defaults: dict[str, object] = dict(
        key=f"summer_league-{idx}",
        name="Summer League",
        event_type=EventType.PRO_SUMMER,
        calendar_source=EventCalendarSource.SCHEDULE,
        calendar_ref={"competition_ids": [1, 2, 3]},
        window_priors={
            "announce_horizon_days": 14,
            "pre_roll_days": 3,
            "gap_bridge_days": 4,
            "post_roll_days": 2,
            "morning_lead_h": 6,
            "morning_floor_et": "09:00",
        },
        cohort_basis="slot_window",
        priority=100,
        is_active=True,
    )
    defaults.update(overrides)
    return Event(**defaults)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_event_roundtrip_persists_json_and_enums(db_session: AsyncSession) -> None:
    """An events row persists calendar_ref/window_priors json and its enums."""
    event = _make_event()
    db_session.add(event)
    await db_session.flush()
    await db_session.refresh(event)

    assert event.id is not None
    assert event.event_type == EventType.PRO_SUMMER
    assert event.calendar_source == EventCalendarSource.SCHEDULE
    assert event.calendar_ref == {"competition_ids": [1, 2, 3]}
    assert event.window_priors["announce_horizon_days"] == 14

    fetched = await db_session.get(Event, event.id)
    assert fetched is not None
    assert fetched.key == event.key
    assert fetched.priority == 100


@pytest.mark.asyncio
async def test_event_key_is_unique(db_session: AsyncSession) -> None:
    """The events.key column enforces a stable, non-reusable series key."""
    event = _make_event(key="summer_league-dup")
    db_session.add(event)
    await db_session.flush()

    duplicate = _make_event(key="summer_league-dup")
    db_session.add(duplicate)
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio
async def test_event_desk_state_roundtrip_and_unique_event(
    db_session: AsyncSession,
) -> None:
    """event_desk_state persists phase/daily-state/hero_ref and enforces one row per event."""
    event = _make_event()
    db_session.add(event)
    await db_session.flush()
    assert event.id is not None

    state = EventDeskState(
        event_id=event.id,
        as_of=datetime(2026, 7, 12, 15, 0, 0),
        lifecycle_phase=EventLifecyclePhase.ACTIVE,
        daily_state=EventDailyState.LIVE,
        is_home_owner=True,
        hero_ref={"kind": "live_duel", "game_id": 42},
        freshness_tick_at=datetime(2026, 7, 12, 15, 0, 0),
        next_tick_eta=datetime(2026, 7, 12, 16, 0, 0),
    )
    db_session.add(state)
    await db_session.flush()
    await db_session.refresh(state)

    assert state.id is not None
    assert state.lifecycle_phase == EventLifecyclePhase.ACTIVE
    assert state.daily_state == EventDailyState.LIVE
    assert state.is_home_owner is True
    assert state.hero_ref == {"kind": "live_duel", "game_id": 42}

    result = await db_session.execute(
        select(EventDeskState).where(EventDeskState.event_id == event.id)
    )
    rows = result.scalars().all()
    assert len(rows) == 1

    duplicate = EventDeskState(
        event_id=event.id,
        lifecycle_phase=EventLifecyclePhase.DORMANT,
    )
    db_session.add(duplicate)
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio
async def test_event_desk_state_daily_state_optional_outside_active(
    db_session: AsyncSession,
) -> None:
    """daily_state stays null when the event's lifecycle phase is not active."""
    event = _make_event(key="summer_league-dormant")
    db_session.add(event)
    await db_session.flush()
    assert event.id is not None

    state = EventDeskState(
        event_id=event.id,
        lifecycle_phase=EventLifecyclePhase.DORMANT,
        daily_state=None,
        is_home_owner=False,
    )
    db_session.add(state)
    await db_session.flush()
    await db_session.refresh(state)

    assert state.id is not None
    assert state.daily_state is None
