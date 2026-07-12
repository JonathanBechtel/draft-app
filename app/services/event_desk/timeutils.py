"""Shared, DST-safe UTC<->US/Eastern conversions for the Event Desk state machines.

The server runs UTC and every stored timestamp (`summer_league_games.tip_datetime`,
`event_desk_state.as_of`, etc.) is naive UTC by repo convention. The one place the
Event Desk framework needs a *real* timezone — not just an offset — is the
Ledger->Morning flip's `MORNING_FLOOR = 09:00 ET` prior (behavior spec §2): Eastern
alternates EDT/EST across the year, so a fixed UTC offset would drift by an hour
across the DST boundary. `zoneinfo.ZoneInfo` resolves the correct offset for any
given date, which is what makes these conversions DST-safe.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")


def ensure_naive_utc(value: datetime) -> datetime:
    """Normalize a datetime to naive UTC (repo convention for stored timestamps).

    Args:
        value: A naive datetime (assumed already UTC, per repo convention) or an
            aware datetime in any timezone.

    Returns:
        A naive ``datetime`` in UTC.
    """
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def to_eastern(value: datetime) -> datetime:
    """Convert a naive-UTC (or aware) datetime to an aware US/Eastern datetime.

    Args:
        value: A naive datetime (assumed UTC) or an aware datetime.

    Returns:
        An aware ``datetime`` in ``America/New_York``, DST-correct for its date.
    """
    aware_utc = (
        value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    )
    return aware_utc.astimezone(EASTERN)


def format_et_clock(value: datetime | None) -> str:
    """Render a naive-UTC (or aware) datetime as an Eastern wall-clock label.

    A 23:00 UTC tip becomes ``"7:00 PM ET"`` (DST-correct); ``None`` yields an
    empty string. Registered as the ``et_time`` Jinja filter so templates never
    format a raw naive-UTC value and mislabel it ET -- the hour is built by hand
    (not ``strftime('%-I')``) to avoid platform-specific strftime flags.
    """
    if value is None:
        return ""
    eastern = to_eastern(value)
    hour12 = eastern.hour % 12 or 12
    ampm = "AM" if eastern.hour < 12 else "PM"
    return f"{hour12}:{eastern.minute:02d} {ampm} ET"


def to_eastern_date(value: datetime) -> date:
    """The Eastern calendar date a naive-UTC (or aware) datetime falls on.

    This is deliberately *not* ``value.date()`` — a late-night Pacific-time Summer
    League game (or, generically, any timestamp within a few hours of UTC midnight)
    can tip on one UTC calendar date but still fall on the *previous* Eastern date,
    which is the schedule convention NBA feeds and `summer_league_games.game_date`
    already use.

    Args:
        value: A naive datetime (assumed UTC) or an aware datetime.

    Returns:
        The corresponding Eastern calendar ``date``.
    """
    return to_eastern(value).date()


def eastern_floor_to_utc(reference_utc: datetime, floor_hhmm: str) -> datetime:
    """Resolve a wall-clock Eastern time-of-day, on `reference_utc`'s Eastern date, as naive UTC.

    Used to compute the Ledger->Morning flip's ``MORNING_FLOOR`` prior (e.g.
    ``"09:00"`` ET) as a concrete, comparable naive-UTC instant for a specific day —
    DST-safe because the Eastern offset is resolved for that day's actual date, not a
    hardcoded UTC delta.

    Args:
        reference_utc: A naive-UTC (or aware) datetime; only its Eastern calendar
            date is used.
        floor_hhmm: A ``"HH:MM"`` 24-hour wall-clock time, e.g. ``"09:00"``.

    Returns:
        The naive-UTC instant of ``floor_hhmm`` Eastern on ``reference_utc``'s
        Eastern date.
    """
    eastern_date = to_eastern_date(reference_utc)
    hour_str, minute_str = floor_hhmm.split(":")
    floor_eastern = datetime(
        eastern_date.year,
        eastern_date.month,
        eastern_date.day,
        int(hour_str),
        int(minute_str),
        tzinfo=EASTERN,
    )
    return ensure_naive_utc(floor_eastern)
