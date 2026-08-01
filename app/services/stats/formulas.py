"""Pure formulas shared by Summer League read and materialization paths.

The functions in this module accept neutral numeric values only.  They deliberately
do not import ORM models or Summer League modules so the stats package remains a
source-agnostic hub.
"""

from __future__ import annotations

from typing import Optional


PACE_MINUTES = 48.0
WS_MINUTES = 40.0
VORP_GAMES = 82.0
VORP_REPLACEMENT = -2.0


def scale_python(
    value: float,
    mode: str,
    *,
    gp: int,
    seconds: float,
    pace_seconds: float,
) -> Optional[float]:
    """Scale a counting-stat total into the selected display mode.

    ``pace_seconds`` is the sum of ``pace * seconds``.  NBA pace is normalized
    to 48 minutes, so the per-100 denominator is ``pace_seconds / (60 * 48)``.
    The result is intentionally unrounded; callers own display precision.
    """
    if mode == "per_game":
        return value / gp if gp else None
    if mode == "per_36":
        return value * 36.0 * 60.0 / seconds if seconds else None
    if mode == "per_100":
        return (
            value * 100.0 * 60.0 * PACE_MINUTES / pace_seconds if pace_seconds else None
        )
    return value


def pace_seconds_from_possessions(possessions: Optional[float]) -> float:
    """Convert possessions to the pace-weighted-seconds denominator."""
    return possessions * 60.0 * PACE_MINUTES if possessions else 0.0


def points_per_100(points: float, possessions: float) -> Optional[float]:
    """Return points per 100 possessions, or ``None`` for no possessions."""
    return 100.0 * points / possessions if possessions else None


def pace_per_48(possessions: float, team_minutes: float) -> Optional[float]:
    """Return pooled possessions per 48 team minutes, or ``None`` if undefined."""
    team_minutes_fifths = team_minutes / 5.0
    return (
        PACE_MINUTES * possessions / team_minutes_fifths
        if team_minutes_fifths
        else None
    )


def net_rating(
    off_rating: Optional[float], def_rating: Optional[float]
) -> Optional[float]:
    """Return a net rating only when both component ratings are available."""
    if off_rating is None or def_rating is None:
        return None
    return off_rating - def_rating


def win_shares_per_40(win_shares: float, minutes: float) -> Optional[float]:
    """Return Win Shares scaled to 40 minutes, or ``None`` without minutes."""
    return WS_MINUTES * win_shares / minutes if minutes else None


def vorp_total(bpm: float, minutes: float) -> float:
    """Return cumulative VORP from BPM and minutes played."""
    return (bpm - VORP_REPLACEMENT) * minutes / (PACE_MINUTES * VORP_GAMES)


def vorp82(bpm: float, percent_available_minutes: float) -> float:
    """Return the full-season VORP projection from available-minute share."""
    return (bpm - VORP_REPLACEMENT) * percent_available_minutes
