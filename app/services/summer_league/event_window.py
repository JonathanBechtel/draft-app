"""Shared Summer League lifecycle-window checks for scheduled jobs."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
import os

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.event_desk import EventLifecyclePhase
from app.schemas.summer_league import SummerLeagueEdition
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


_FALSY_ENV_VALUES = frozenset({"0", "false", "no", "off"})


def default_roster_year() -> int:
    """Return the current Eastern calendar year as the roster-poll default.

    Mirrors the today's-year fallback ``resolve_target_competitions`` already
    uses (``app/services/summer_league/scoreboard_ingest.py``) so the cron
    follows the calendar without a code change each season -- no more
    hard-coded season year to bump every summer.
    """
    return to_eastern_date(datetime.now(timezone.utc)).year


def resolve_roster_year() -> int:
    """Resolve the Summer League year, defaulting to the current season.

    Raises:
        ValueError: If ``SL_ROSTER_YEAR`` is set but is not a plausible
            four-digit season year. Failing here (rather than deep inside a
            per-venue fetch) makes a misconfigured schedule fail loudly with
            a non-zero exit code instead of silently fetching nothing for
            every venue.
    """
    raw = os.getenv("SL_ROSTER_YEAR")
    if not raw or not raw.strip():
        return default_roster_year()
    stripped = raw.strip()
    try:
        year = int(stripped)
    except ValueError as exc:
        raise ValueError(
            f"SL_ROSTER_YEAR must be a four-digit year, got {stripped!r}"
        ) from exc
    if not 1900 <= year <= 2100:
        raise ValueError(
            f"SL_ROSTER_YEAR must be a four-digit year in [1900, 2100], got {year}"
        )
    return year


def has_force_override() -> bool:
    """Return whether the operator explicitly forced the window bypass.

    ``SL_ROSTER_FORCE=1`` is the explicit, standalone escape hatch for
    operator-directed backfills. It is distinct from ``SL_ROSTER_YEAR``,
    which only scopes *which* year's competitions the window check
    considers -- setting a year no longer implies bypassing the gate.
    """
    raw = os.getenv("SL_ROSTER_FORCE")
    if not raw or not raw.strip():
        return False
    return raw.strip().lower() not in _FALSY_ENV_VALUES


def synthetic_schedule_dates(
    competitions: Sequence[SummerLeagueEdition],
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


async def is_summer_league_window_open(
    db: AsyncSession, *, now: datetime, year: int | None = None
) -> bool:
    """Return whether a Summer League scheduled job may make network calls.

    The check is read-only: it uses the same competition resolver, lifecycle
    state machine, and registration priors as the Event Desk, while synthetic
    competition date windows keep the pre-game polling window available before
    any ``summer_league_games`` rows exist.

    ``SL_ROSTER_YEAR`` (surfaced here via the ``year`` argument) only scopes
    *which* year's competitions are considered -- it does not bypass the
    gate. ``SL_ROSTER_FORCE=1`` is the explicit, separate bypass for
    operator-directed backfills.

    Args:
        db: Async database session.
        now: Timestamp used to evaluate the lifecycle phase.
        year: Optional roster year to scope the resolved competitions.
    """
    if has_force_override():
        return True
    competitions = await resolve_target_competitions(db, today=to_eastern_date(now))
    if year is not None:
        competitions = [
            competition for competition in competitions if competition.year == year
        ]
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
