"""Generic Event Desk registry + state tables.

The Event Desk framework (`docs/plans/event-desk-framework.md`) is the event-agnostic
scaffolding the Summer League Desk (event-instance #1) plugs into — and that FIBA / AAU /
U17 / March Madness plug into later. It runs two nested state machines:

* **Outer — Event Lifecycle** (per event): ``dormant -> announced -> warmup -> active ->
  winddown -> archived``, driven by each event's calendar + window priors.
* **Inner — Daily Coverage** (only while ``active``): ``preview -> live -> recap`` — the SL
  Morning/Live/Ledger machine, renamed event-neutral.

Two tables hold the framework's state:

* :class:`Event` — the registry row for a pluggable event series (e.g. ``summer_league``).
  Per the framework doc's "entity mapping guardrail", this is a **desk-control registry,
  not a parallel sports data model** — ``calendar_ref`` points back at existing schedule
  data (for Summer League, the ``summer_league_competitions.id`` the event tracks) rather
  than duplicating identity/schedule records.
* :class:`EventDeskState` — the upserted-each-tick snapshot of an event's current lifecycle
  phase, daily state, home-page ownership, hero reference, and freshness. Consumed by the
  EventDesk controller (ticket #506) to decide what the home page renders.

SL's content projections (T1-T4, `app/schemas/summer_league_desk.py`) are unaffected --
this module is framework-level only and holds no SL-specific columns.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import (
    Column,
    Enum as SAEnum,
    Index,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class EventType(str, Enum):
    """The category of event a registered :class:`Event` row represents.

    Values drawn from the framework doc's priority scale (March Madness 100, Summer
    League 80, Combine 70, FIBA senior 65, U17/U19 55, AAU/EYBL 40) plus the SL concrete
    config example (``event_type: "pro_summer"``).
    """

    PRO_SUMMER = "pro_summer"
    MARCH_MADNESS = "march_madness"
    COMBINE = "combine"
    FIBA_SENIOR = "fiba_senior"
    U17_U19 = "u17_u19"
    AAU_EYBL = "aau_eybl"


class EventCalendarSource(str, Enum):
    """How an event's schedule is derived (framework doc "the seam: an Event registry")."""

    SCHEDULE = "schedule"
    CONFIG = "config"


class EventLifecyclePhase(str, Enum):
    """Outer state machine phase (framework doc "Outer -- Event Lifecycle")."""

    DORMANT = "dormant"
    ANNOUNCED = "announced"
    WARMUP = "warmup"
    ACTIVE = "active"
    WINDDOWN = "winddown"
    ARCHIVED = "archived"


class EventDailyState(str, Enum):
    """Inner state machine state, populated only while the event is ``active``."""

    PREVIEW = "preview"
    LIVE = "live"
    RECAP = "recap"


def _enum_column(enum_cls: type[Enum], name: str, *, nullable: bool = False) -> Column:
    """Build a Postgres-backed SAEnum column that serializes on member value."""
    return Column(
        SAEnum(
            enum_cls,
            name=name,
            values_callable=lambda enum_cls: [item.value for item in enum_cls],
        ),
        nullable=nullable,
    )


class Event(SQLModel, table=True):  # type: ignore[call-arg]
    """A registered, pluggable event series the Event Desk controller can own the home page for.

    One row per event *series* (not per occurrence) -- e.g. a single ``summer_league`` row
    persists across years; its lifecycle phase and calendar window are recomputed from
    ``calendar_ref`` + ``window_priors`` on each controller tick, not re-registered.
    """

    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("key", name="uq_events_key"),
        Index("ix_events_is_active_priority", "is_active", "priority"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    # Stable series key, e.g. "summer_league". Never re-used across event types.
    key: str = Field(nullable=False)
    name: str = Field(nullable=False)
    event_type: EventType = Field(sa_column=_enum_column(EventType, "event_type_enum"))

    calendar_source: EventCalendarSource = Field(
        sa_column=_enum_column(EventCalendarSource, "event_calendar_source_enum")
    )
    # Schedule filter (calendar_source="schedule") or config date range
    # (calendar_source="config"). For Summer League this points at the existing
    # summer_league_competitions rows -- never a parallel event-only schedule
    # (framework doc "Entity mapping guardrail").
    calendar_ref: dict[str, object] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    # { announce_horizon_days, pre_roll_days, gap_bridge_days, post_roll_days,
    #   morning_lead_h, morning_floor_et }
    window_priors: dict[str, object] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )

    # How "vs cohort" is computed for this event, e.g. "slot_window" for SL.
    cohort_basis: str = Field(nullable=False)
    # Static home-page precedence weight; highest priority among home-eligible
    # events wins single ownership (framework doc "Overlap precedence").
    priority: int = Field(default=0, nullable=False)
    is_active: bool = Field(default=True, nullable=False)

    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)


class EventDeskState(SQLModel, table=True):  # type: ignore[call-arg]
    """The upserted-each-tick snapshot of a registered event's desk state.

    One row per event, overwritten on every EventDesk controller tick (ticket #506) --
    this is a freshness/state cache, not an append-only history.
    """

    __tablename__ = "event_desk_state"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_event_desk_state_event"),
        Index("ix_event_desk_state_home_owner", "is_home_owner"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    event_id: int = Field(foreign_key="events.id")
    as_of: datetime = Field(default_factory=datetime.utcnow, nullable=False)

    lifecycle_phase: EventLifecyclePhase = Field(
        sa_column=_enum_column(EventLifecyclePhase, "event_lifecycle_phase_enum")
    )
    # Only populated while lifecycle_phase == active.
    daily_state: Optional[EventDailyState] = Field(
        default=None,
        sa_column=_enum_column(
            EventDailyState, "event_daily_state_enum", nullable=True
        ),
    )
    # True when this event currently owns the home-page takeover (single owner
    # across all registered events, by priority -- framework doc "EventDesk
    # controller").
    is_home_owner: bool = Field(default=False, nullable=False)
    # Reference to whatever content is currently featured (e.g. the winning
    # storyline/game/player) -- shape is provider-defined, opaque to the framework.
    hero_ref: Optional[dict[str, object]] = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )

    freshness_tick_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
    next_tick_eta: Optional[datetime] = Field(default=None)
