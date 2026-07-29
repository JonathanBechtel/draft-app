"""Shared Summer League lifecycle-window checks for scheduled jobs."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.event_desk import EventLifecyclePhase
from app.schemas.summer_league import SummerLeagueCompetition
from app.services.event_desk.lifecycle import lifecycle_phase
from app.services.event_desk.registry import DeskEvent, SUMMER_LEAGUE_REGISTRATION
from app.services.event_desk.timeutils import to_eastern_date
from app.services.summer_league.scoreboard_ingest import resolve_target_competitions


SCHEDULE_ELIGIBLE_PHASES = frozenset(
    {
        EventLifecyclePhase.ANNOUNCED,
        EventLifecyclePhase.WARMUP,
        EventLifecyclePhase.ACTIVE,
        EventLifecyclePhase.WINDDOWN,
    }
)


def synthetic_schedule_dates(
    competitions: Sequence[SummerLeagueCompetition],
) -> tuple[date, ...]:
    """Expand configured competition date windows into a lifecycle calendar."""
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


async def is_summer_league_window_open(db: AsyncSession, *, now: datetime) -> bool:
    """Return whether a Summer League scheduled job may make network calls.

    The check is read-only: it uses the same competition resolver, lifecycle
    state machine, and registration priors as the Event Desk, while synthetic
    competition date windows keep the pre-game polling window available before
    any ``summer_league_games`` rows exist.
    """
    competitions = await resolve_target_competitions(db, today=to_eastern_date(now))
    synthetic_dates = synthetic_schedule_dates(competitions)
    if not synthetic_dates:
        return False
    desk_event = DeskEvent(
        key=SUMMER_LEAGUE_REGISTRATION.key,
        priority=SUMMER_LEAGUE_REGISTRATION.priority,
        window_priors=SUMMER_LEAGUE_REGISTRATION.window_priors,
        game_dates=synthetic_dates,
    )
    return lifecycle_phase(now, desk_event) in SCHEDULE_ELIGIBLE_PHASES
