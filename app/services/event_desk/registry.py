"""Event registry — the "Event object" + content-provider interface.

`docs/plans/event-desk-framework.md` ("The seam: an Event registry") specs a
generic registry where the two state machines are shared and a registered *Event
object* is what varies per event (calendar source/ref, window priors, cohort basis,
priority, content providers). This module holds that seam:

* :class:`WindowPriors` — the per-event lifecycle knobs (`announce_horizon_days` etc).
* :class:`DeskEvent` — the pure, tick-scoped object `lifecycle.py`/`state_machine.py`
  reason about (window priors + this tick's resolved calendar facts). Rebuilt fresh
  every tick; never persisted itself.
* :class:`GameStatus` — an event-agnostic game-status enum the inner state machine
  consumes, so `state_machine.py` never imports Summer-League-specific schemas.
* :class:`DeskContentProvider` — the per-event content-provider Protocol. V1 wires
  only `resolve_calendar_facts` (what the state machines need); hero/storyline/spine
  rendering are later tickets' job, implemented against this same seam.
* :data:`REGISTERED_EVENTS` — V1 registers **Summer League only** (behavior spec §9 /
  framework doc "V1 scope"). Event #2 (FIBA/AAU/U17/March Madness) is config, not a
  refactor, once a second example exists to generalize the SL-only bits below.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from enum import Enum
from typing import Awaitable, Callable, Mapping, Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.event_desk import Event, EventCalendarSource, EventType
from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeagueGame,
    SummerLeagueGameStatus,
)
from app.services.summer_league.scoreboard_ingest import (
    EVENT_KEY_SUMMER_LEAGUE,
    resolve_target_competitions,
)
from app.services.event_desk.timeutils import to_eastern_date

# --------------------------------------------------------------------------
# Event-agnostic status + window-priors types
# --------------------------------------------------------------------------


class GameStatus(str, Enum):
    """Event-agnostic game status the inner state machine (`state_machine.py`) reasons about.

    Values mirror :class:`~app.schemas.summer_league.SummerLeagueGameStatus` 1:1 so a
    per-event provider maps its native status enum with a trivial
    ``GameStatus(status.value)`` cast (see :func:`_to_generic_status` below) — kept as
    a distinct type so the inner state machine stays reusable by a future non-SL event
    without importing Summer-League-specific schemas.
    """

    SCHEDULED = "scheduled"
    IN_PROGRESS = "in_progress"
    FINAL = "final"
    UNKNOWN = "unknown"


def _to_generic_status(status: SummerLeagueGameStatus) -> GameStatus:
    """Map a Summer-League-native game status onto the event-agnostic :class:`GameStatus`."""
    return GameStatus(status.value)


@dataclass(frozen=True)
class WindowPriors:
    """Per-event calendar/lifecycle knobs (framework doc "window priors").

    Mirrors ``events.window_priors`` (JSONB) 1:1; :meth:`from_dict` is the read-side
    of that JSON contract.
    """

    announce_horizon_days: int = 14
    pre_roll_days: int = 3
    gap_bridge_days: int = 4
    post_roll_days: int = 2
    morning_lead_h: float = 6.0
    morning_floor_et: str = "09:00"

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "WindowPriors":
        """Build :class:`WindowPriors` from a stored ``events.window_priors`` dict.

        Args:
            data: The raw JSONB mapping; missing keys fall back to this class's
                defaults (the SL priors pinned in behavior spec §2 / framework doc).

        Returns:
            A populated, immutable :class:`WindowPriors`.
        """
        defaults = cls()
        announce_horizon_days = data.get(
            "announce_horizon_days", defaults.announce_horizon_days
        )
        pre_roll_days = data.get("pre_roll_days", defaults.pre_roll_days)
        gap_bridge_days = data.get("gap_bridge_days", defaults.gap_bridge_days)
        post_roll_days = data.get("post_roll_days", defaults.post_roll_days)
        morning_lead_h = data.get("morning_lead_h", defaults.morning_lead_h)
        morning_floor_et = data.get("morning_floor_et", defaults.morning_floor_et)
        return cls(
            announce_horizon_days=int(announce_horizon_days),  # type: ignore[call-overload]
            pre_roll_days=int(pre_roll_days),  # type: ignore[call-overload]
            gap_bridge_days=int(gap_bridge_days),  # type: ignore[call-overload]
            post_roll_days=int(post_roll_days),  # type: ignore[call-overload]
            morning_lead_h=float(morning_lead_h),  # type: ignore[arg-type]
            morning_floor_et=str(morning_floor_et),
        )


@dataclass(frozen=True)
class DeskEvent:
    """The pure, tick-scoped object `lifecycle_phase`/`inner_state` reason about.

    Bundles one registered event's static window priors with the calendar facts
    resolved for *this* tick (`CalendarFacts.game_dates`). Rebuilt fresh every tick
    by the controller — never persisted itself; the DB `events`/`event_desk_state`
    rows are the persisted counterparts.
    """

    key: str
    priority: int
    window_priors: WindowPriors
    game_dates: tuple[date, ...]


@dataclass(frozen=True)
class CalendarFacts:
    """One event's calendar facts for the current tick, resolved by its content provider."""

    # Every known calendar date with >=1 scheduled/played game (outer lifecycle
    # clustering — `lifecycle.py`'s gap-bridge algorithm).
    game_dates: tuple[date, ...]
    # Naive-UTC tip times for *today*'s (Eastern-date) games (inner state machine).
    today_schedule: tuple[datetime, ...]
    # Game statuses for today's games, in the event-agnostic `GameStatus` vocabulary.
    # Not required to be pairwise-aligned with `today_schedule` (a game missing a
    # `tip_datetime` still contributes its status; see `_SummerLeagueCalendarProvider`).
    today_statuses: tuple[GameStatus, ...]


class DeskContentProvider(Protocol):
    """Per-event content-provider interface (framework doc "content_providers").

    V1 (#506) wires only `resolve_calendar_facts` — the one method the two state
    machines need. Hero/storyline/spine/daily-state rendering (the framework doc's
    other `content_providers` entries: hero, storyline_triggers, spine, daily_states)
    are implemented by later tickets (#504 storyline engine, #508 desk read service)
    against this same seam; this Protocol is the plug point they extend.
    """

    async def resolve_calendar_facts(
        self, db: AsyncSession, *, now: datetime
    ) -> CalendarFacts: ...


@dataclass(frozen=True)
class EventRegistration:
    """One event's static registration: config + how to sync/resolve it each tick."""

    key: str
    name: str
    event_type: EventType
    calendar_source: EventCalendarSource
    cohort_basis: str
    priority: int
    window_priors: WindowPriors
    provider: DeskContentProvider
    # Idempotently create/refresh this event's `events` row for `today` and return it.
    sync: Callable[[AsyncSession, date], Awaitable[Event]]


# --------------------------------------------------------------------------
# Generic `events` row upsert (shared by every registration's `sync`)
# --------------------------------------------------------------------------


async def _upsert_event_row(
    registration: "EventRegistration",
    db: AsyncSession,
    *,
    calendar_ref: dict[str, object],
) -> Event:
    """Idempotently create/refresh one event's `events` registry row.

    Args:
        registration: The static registration whose config fields populate the row.
        db: Active database session (caller controls the transaction).
        calendar_ref: This tick's resolved ``calendar_ref`` payload (e.g. SL's
            ``{"competition_ids": [...]}``).

    Returns:
        The persisted :class:`~app.schemas.event_desk.Event` row.
    """
    base_values: dict[str, object] = {
        "key": registration.key,
        "name": registration.name,
        "event_type": registration.event_type,
        "calendar_source": registration.calendar_source,
        "calendar_ref": calendar_ref,
        "window_priors": asdict(registration.window_priors),
        "cohort_basis": registration.cohort_basis,
        "priority": registration.priority,
        "is_active": True,
    }
    insert_values = {
        **base_values,
        "created_at": datetime.now(timezone.utc).replace(tzinfo=None),
    }
    update_values = {k: v for k, v in base_values.items() if k != "key"}

    stmt = insert(Event).values(**insert_values)
    stmt = stmt.on_conflict_do_update(constraint="uq_events_key", set_=update_values)
    await db.execute(stmt)
    await db.flush()

    result = await db.execute(select(Event).where(Event.key == registration.key))  # type: ignore[arg-type]
    return result.scalar_one()


# --------------------------------------------------------------------------
# Summer League registration (event-instance #1)
# --------------------------------------------------------------------------

SUMMER_LEAGUE_WINDOW_PRIORS = WindowPriors(
    announce_horizon_days=14,
    pre_roll_days=3,
    gap_bridge_days=4,
    post_roll_days=2,
    morning_lead_h=6.0,
    morning_floor_et="09:00",
)


async def _resolve_current_year_summer_league_competitions(
    db: AsyncSession, *, today: date
) -> list[SummerLeagueCompetition]:
    """Every `summer_league_competitions` row for `today`'s year (registration source)."""
    stmt = select(SummerLeagueCompetition).where(
        SummerLeagueCompetition.year == today.year  # type: ignore[arg-type]
    )
    return list((await db.execute(stmt)).scalars().all())


async def sync_summer_league_event(db: AsyncSession, today: date) -> Event:
    """Idempotently create/refresh the SL `events` registry row for `today`'s competitions.

    Self-healing: recomputes ``calendar_ref["competition_ids"]`` from `today.year`'s
    `summer_league_competitions` on every call (never read back from the prior row),
    so a newly-created competition mid-season (e.g. Vegas added after CA Classic +
    SLC already exist) is picked up automatically on the next tick.
    :func:`~app.services.summer_league.scoreboard_ingest.resolve_target_competitions`
    (#515) reads this row back via its "prefer active events row" path once it exists.

    Args:
        db: Active database session (caller controls the transaction).
        today: The current date (Eastern or UTC calendar date — only the year
            matters here), used to select this year's competitions.

    Returns:
        The persisted :class:`~app.schemas.event_desk.Event` row.
    """
    competitions = await _resolve_current_year_summer_league_competitions(
        db, today=today
    )
    calendar_ref: dict[str, object] = {
        "competition_ids": [c.id for c in competitions if c.id is not None]
    }
    return await _upsert_event_row(
        SUMMER_LEAGUE_REGISTRATION, db, calendar_ref=calendar_ref
    )


class _SummerLeagueCalendarProvider:
    """SL's :class:`DeskContentProvider` — resolves calendar facts from `summer_league_games`.

    The only Summer-League-specific glue in the framework layer: everything else in
    `lifecycle.py`/`state_machine.py` is event-agnostic and takes plain dates/statuses.
    """

    async def resolve_calendar_facts(
        self, db: AsyncSession, *, now: datetime
    ) -> CalendarFacts:
        """Resolve SL's calendar facts for one tick.

        Args:
            db: Active database session.
            now: The tick's reference instant (naive UTC).

        Returns:
            :class:`CalendarFacts` — every known SL game date (for the outer
            lifecycle's gap-bridge clustering) plus today's (Eastern-date) tip
            schedule and game statuses (for the inner state machine).
        """
        today = to_eastern_date(now)
        competitions = await resolve_target_competitions(db, today=today)
        competition_ids = [c.id for c in competitions if c.id is not None]
        if not competition_ids:
            return CalendarFacts(game_dates=(), today_schedule=(), today_statuses=())

        dates_stmt = select(SummerLeagueGame.game_date).where(  # type: ignore[call-overload]
            SummerLeagueGame.competition_id.in_(competition_ids),  # type: ignore[attr-defined]
            SummerLeagueGame.game_date.is_not(None),  # type: ignore[union-attr]
        )
        game_dates = tuple(
            sorted({row[0] for row in (await db.execute(dates_stmt)).all()})
        )

        today_stmt = select(  # type: ignore[call-overload]
            SummerLeagueGame.tip_datetime, SummerLeagueGame.status
        ).where(
            SummerLeagueGame.competition_id.in_(competition_ids),  # type: ignore[attr-defined]
            SummerLeagueGame.game_date == today,  # type: ignore[arg-type]
        )
        today_rows = (await db.execute(today_stmt)).all()
        today_schedule = tuple(tip for tip, _status in today_rows if tip is not None)
        today_statuses = tuple(
            _to_generic_status(status) for _tip, status in today_rows
        )

        return CalendarFacts(
            game_dates=game_dates,
            today_schedule=today_schedule,
            today_statuses=today_statuses,
        )


SUMMER_LEAGUE_REGISTRATION = EventRegistration(
    key=EVENT_KEY_SUMMER_LEAGUE,
    name="Summer League",
    event_type=EventType.PRO_SUMMER,
    calendar_source=EventCalendarSource.SCHEDULE,
    cohort_basis="slot_window",
    priority=100,  # unopposed in V1 (framework doc "SL as event instance #1")
    window_priors=SUMMER_LEAGUE_WINDOW_PRIORS,
    provider=_SummerLeagueCalendarProvider(),
    sync=sync_summer_league_event,
)

# V1 registers Summer League only (behavior spec §9 / framework doc "V1 scope").
REGISTERED_EVENTS: tuple[EventRegistration, ...] = (SUMMER_LEAGUE_REGISTRATION,)
