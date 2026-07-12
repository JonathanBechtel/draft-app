"""Outer state machine — per-event lifecycle phase (pure).

`docs/plans/event-desk-framework.md` ("Outer — Event Lifecycle"):

    Dormant -> Announced -> Warm-up -> Active -> Wind-down -> Archived

driven by each event's calendar (known game dates) + window priors
(`announce_horizon_days` / `pre_roll_days` / `gap_bridge_days` / `post_roll_days`).
`gap_bridge_days` is what keeps a multi-sub-event cluster (Summer League's CA
Classic -> Salt Lake City -> Las Vegas) one contiguous Active window instead of
flickering to Wind-down and back between venues.

Both functions in this module are pure — no I/O, no clock reads — so they're
directly table-driven-testable and reusable by a future non-SL event.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional, Sequence

from app.schemas.event_desk import EventLifecyclePhase
from app.services.event_desk.registry import DeskEvent
from app.services.event_desk.timeutils import to_eastern_date


def cluster_game_dates(
    dates: Sequence[date], gap_bridge_days: int
) -> list[tuple[date, date]]:
    """Group calendar dates into contiguous clusters, bridging gaps <= `gap_bridge_days`.

    Two consecutive known game dates fall in the *same* cluster when the gap between
    them is `<= gap_bridge_days`; a strictly larger gap starts a new cluster. This is
    the mechanism behind the framework doc's gap-bridge guarantee: Summer League's
    CA Classic -> SLC -> Vegas dates, whose inter-venue gaps are all `<=
    gap_bridge_days` (4), collapse into a single `(min_date, max_date)` cluster —
    one contiguous Active window for the whole event, not three flickering ones.

    Args:
        dates: Every known calendar date with >=1 scheduled/played game for the
            event (duplicates and unsorted order are fine).
        gap_bridge_days: The event's `gap_bridge_days` window prior.

    Returns:
        Sorted, non-overlapping `(cluster_start, cluster_end)` pairs, oldest first.
        Empty when `dates` is empty.
    """
    unique_sorted = sorted(set(dates))
    if not unique_sorted:
        return []

    clusters: list[tuple[date, date]] = []
    cluster_start = unique_sorted[0]
    prev = unique_sorted[0]
    for current in unique_sorted[1:]:
        if (current - prev).days > gap_bridge_days:
            clusters.append((cluster_start, prev))
            cluster_start = current
        prev = current
    clusters.append((cluster_start, prev))
    return clusters


def lifecycle_phase(now: datetime, event: DeskEvent) -> EventLifecyclePhase:
    """Resolve one event's outer lifecycle phase at `now`.

    Args:
        now: The tick/request instant (naive UTC, per repo convention). Compared
            against `event.game_dates` on its **Eastern** calendar date (NBA
            schedule convention — see `timeutils.to_eastern_date`), so a Vegas game
            tipping late Pacific-time doesn't get misdated by a bare UTC `.date()`.
        event: The event's window priors + every known calendar date for the
            current cluster set (:class:`~app.services.event_desk.registry.DeskEvent`).

    Returns:
        The single current :class:`~app.schemas.event_desk.EventLifecyclePhase`.
        `DORMANT` both when the event is far off *and* as the safe fallback for a gap
        between two clusters wider than any window prior covers (multi-cluster
        overlap precedence beyond that is deferred to event #2 per the framework
        doc's V1 scope note — moot for SL, which is always one bridged cluster).
    """
    today = to_eastern_date(now)
    priors = event.window_priors
    clusters = cluster_game_dates(event.game_dates, priors.gap_bridge_days)
    if not clusters:
        return EventLifecyclePhase.DORMANT

    for cluster_start, cluster_end in clusters:
        active_end = cluster_end + timedelta(days=priors.post_roll_days)
        warmup_start = cluster_start - timedelta(days=priors.pre_roll_days)
        announced_start = cluster_start - timedelta(days=priors.announce_horizon_days)

        if cluster_start <= today <= cluster_end:
            return EventLifecyclePhase.ACTIVE
        if cluster_end < today <= active_end:
            return EventLifecyclePhase.WINDDOWN
        if warmup_start <= today < cluster_start:
            return EventLifecyclePhase.WARMUP
        if announced_start <= today < warmup_start:
            return EventLifecyclePhase.ANNOUNCED

    first_cluster_start = clusters[0][0]
    last_cluster_end = clusters[-1][1]
    if today < first_cluster_start - timedelta(days=priors.announce_horizon_days):
        return EventLifecyclePhase.DORMANT
    if today > last_cluster_end + timedelta(days=priors.post_roll_days):
        return EventLifecyclePhase.ARCHIVED
    # Falls between two clusters, farther out than either cluster's own
    # announce/warmup/winddown windows reach -- treat as Dormant (see docstring).
    return EventLifecyclePhase.DORMANT


_HOME_ELIGIBLE_PHASES = (
    EventLifecyclePhase.WARMUP,
    EventLifecyclePhase.ACTIVE,
    EventLifecyclePhase.WINDDOWN,
)
# Active beats Warm-up beats Wind-down when priority alone doesn't decide
# (framework doc "Overlap precedence": "prefer live/active content over teasers").
_PHASE_RANK = {
    EventLifecyclePhase.ACTIVE: 2,
    EventLifecyclePhase.WARMUP: 1,
    EventLifecyclePhase.WINDDOWN: 0,
}


def _nearest_game_distance(today: date, event: DeskEvent) -> int:
    """Absolute days from `today` to the nearest known game date (tie-break only)."""
    if not event.game_dates:
        return 10**9
    return min(abs((game_date - today).days) for game_date in event.game_dates)


def resolve_home_owner(
    now: datetime, events: Sequence[DeskEvent]
) -> Optional[DeskEvent]:
    """Pick the single event that owns the home-page takeover at `now`.

    Framework doc "EventDesk controller" / "Overlap precedence": collect
    home-eligible events (phase in {Warm-up, Active, Wind-down}); the highest
    `priority` among them wins single ownership, tie-broken by phase rank (Active >
    Warm-up > Wind-down) then nearest game date. Returns `None` when no registered
    event is home-eligible (standard home page renders).

    Args:
        now: The tick/request instant (naive UTC).
        events: Every registered event's tick-scoped :class:`DeskEvent`. V1
            registers Summer League only, so this is a 1-tuple and the function is
            trivially unopposed — but it is written to resolve N events, the seam
            the framework doc calls out for event #2.

    Returns:
        The winning :class:`DeskEvent`, or `None` if none is home-eligible.
    """
    today = to_eastern_date(now)
    phases = [(event, lifecycle_phase(now, event)) for event in events]
    eligible = [item for item in phases if item[1] in _HOME_ELIGIBLE_PHASES]
    if not eligible:
        return None

    def _sort_key(item: tuple[DeskEvent, EventLifecyclePhase]) -> tuple[int, int, int]:
        event, phase = item
        return (
            event.priority,
            _PHASE_RANK[phase],
            -_nearest_game_distance(today, event),
        )

    winner, _phase = max(eligible, key=_sort_key)
    return winner
