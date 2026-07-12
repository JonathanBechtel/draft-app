"""Table-driven unit tests for the outer Event Lifecycle resolver (pure, no DB).

Covers: every phase transition, the gap-bridge guarantee (CA Classic -> SLC ->
Vegas collapsing into one contiguous Active window), a UTC-vs-Eastern calendar-date
boundary (a late-tip game whose UTC date and Eastern date disagree), and
`resolve_home_owner`'s single-owner-by-priority + tie-break behavior.
"""

from __future__ import annotations

from datetime import date, datetime

from app.schemas.event_desk import EventLifecyclePhase
from app.services.event_desk.lifecycle import (
    cluster_game_dates,
    lifecycle_phase,
    resolve_home_owner,
)
from app.services.event_desk.registry import DeskEvent, WindowPriors

_PRIORS = WindowPriors(
    announce_horizon_days=14,
    pre_roll_days=3,
    gap_bridge_days=4,
    post_roll_days=2,
    morning_lead_h=6.0,
    morning_floor_et="09:00",
)


def _sl_like_event(game_dates: tuple[date, ...], *, priority: int = 100) -> DeskEvent:
    return DeskEvent(
        key="summer_league", priority=priority, window_priors=_PRIORS, game_dates=game_dates
    )


class TestClusterGameDates:
    """Direct coverage of the gap-bridge clustering primitive."""

    def test_empty_dates_yield_no_clusters(self) -> None:
        assert cluster_game_dates([], gap_bridge_days=4) == []

    def test_single_date_is_one_cluster(self) -> None:
        assert cluster_game_dates([date(2026, 7, 10)], gap_bridge_days=4) == [
            (date(2026, 7, 10), date(2026, 7, 10))
        ]

    def test_gap_within_bridge_days_merges_clusters(self) -> None:
        """CA Classic (Jul 5-7) -> SLC (Jul 9-10) -> Vegas (Jul 12-20, every-other-day
        slate): every inter-venue/inter-game gap is <= gap_bridge_days=4, so this is
        ONE contiguous window."""
        dates = [
            date(2026, 7, 5),
            date(2026, 7, 6),
            date(2026, 7, 7),
            date(2026, 7, 9),
            date(2026, 7, 10),
            date(2026, 7, 12),
            date(2026, 7, 14),
            date(2026, 7, 16),
            date(2026, 7, 18),
            date(2026, 7, 20),
        ]
        clusters = cluster_game_dates(dates, gap_bridge_days=4)
        assert clusters == [(date(2026, 7, 5), date(2026, 7, 20))]

    def test_gap_beyond_bridge_days_splits_clusters(self) -> None:
        dates = [date(2026, 7, 5), date(2026, 7, 6), date(2026, 8, 1)]
        clusters = cluster_game_dates(dates, gap_bridge_days=4)
        assert clusters == [
            (date(2026, 7, 5), date(2026, 7, 6)),
            (date(2026, 8, 1), date(2026, 8, 1)),
        ]


class TestLifecyclePhase:
    """Table-driven phase resolution against a single SL-like cluster (Jul 5-20)."""

    _GAME_DATES = (
        date(2026, 7, 5),
        date(2026, 7, 6),
        date(2026, 7, 7),
        date(2026, 7, 9),
        date(2026, 7, 10),
        date(2026, 7, 12),
        date(2026, 7, 14),
        date(2026, 7, 16),
        date(2026, 7, 18),
        date(2026, 7, 20),
    )

    def test_no_game_dates_is_dormant(self) -> None:
        event = _sl_like_event(())
        assert lifecycle_phase(datetime(2026, 7, 10, 12, 0), event) == EventLifecyclePhase.DORMANT

    def test_far_off_is_dormant(self) -> None:
        event = _sl_like_event(self._GAME_DATES)
        # More than announce_horizon_days (14) before the cluster starts (Jul 5).
        assert (
            lifecycle_phase(datetime(2026, 6, 1, 12, 0), event) == EventLifecyclePhase.DORMANT
        )

    def test_within_announce_horizon_is_announced(self) -> None:
        event = _sl_like_event(self._GAME_DATES)
        # Jun 25 is 10 days before Jul 5 -- inside the 14-day horizon, outside the
        # 3-day pre-roll.
        assert (
            lifecycle_phase(datetime(2026, 6, 25, 12, 0), event) == EventLifecyclePhase.ANNOUNCED
        )

    def test_within_pre_roll_is_warmup(self) -> None:
        event = _sl_like_event(self._GAME_DATES)
        # Jul 3 is 2 days before Jul 5 -- inside the 3-day pre-roll window.
        assert lifecycle_phase(datetime(2026, 7, 3, 12, 0), event) == EventLifecyclePhase.WARMUP

    def test_first_game_day_is_active(self) -> None:
        event = _sl_like_event(self._GAME_DATES)
        assert lifecycle_phase(datetime(2026, 7, 5, 12, 0), event) == EventLifecyclePhase.ACTIVE

    def test_bridged_gap_day_is_still_active(self) -> None:
        """Jul 11 has no game (a gap day between SLC and Vegas) but sits inside the
        one bridged cluster's [Jul 5, Jul 20] span -- must stay Active, not flicker
        to Wind-down/Warm-up."""
        event = _sl_like_event(self._GAME_DATES)
        assert lifecycle_phase(datetime(2026, 7, 11, 12, 0), event) == EventLifecyclePhase.ACTIVE

    def test_last_game_day_is_active(self) -> None:
        event = _sl_like_event(self._GAME_DATES)
        assert lifecycle_phase(datetime(2026, 7, 20, 12, 0), event) == EventLifecyclePhase.ACTIVE

    def test_within_post_roll_is_winddown(self) -> None:
        event = _sl_like_event(self._GAME_DATES)
        # Jul 22 is 2 days after the Jul 20 finale -- inside the 2-day post-roll tail.
        assert (
            lifecycle_phase(datetime(2026, 7, 22, 12, 0), event) == EventLifecyclePhase.WINDDOWN
        )

    def test_after_post_roll_is_archived(self) -> None:
        event = _sl_like_event(self._GAME_DATES)
        assert (
            lifecycle_phase(datetime(2026, 8, 1, 12, 0), event) == EventLifecyclePhase.ARCHIVED
        )

    def test_utc_vs_eastern_date_boundary_shifts_phase(self) -> None:
        """A `now` timestamp near UTC midnight can fall on a different Eastern
        calendar date -- using the wrong one would wrongly resolve Active a day
        early. 2026-07-10 02:00 UTC is 2026-07-09 22:00 EDT (July = EDT, UTC-4)."""
        event = _sl_like_event((date(2026, 7, 10),))
        now_utc = datetime(2026, 7, 10, 2, 0)  # ET calendar date is still Jul 9.
        # pre_roll_days=3: Jul 9 (ET "today") is within [Jul 7, Jul 10) -> Warm-up,
        # not Active (which a naive UTC .date() comparison would wrongly yield).
        assert lifecycle_phase(now_utc, event) == EventLifecyclePhase.WARMUP

    def test_utc_vs_eastern_date_boundary_after_flip_is_active(self) -> None:
        """The mirror case: a `now` a few hours later, once the ET date has rolled
        to the game date itself, correctly resolves Active."""
        event = _sl_like_event((date(2026, 7, 10),))
        now_utc = datetime(2026, 7, 10, 6, 0)  # 02:00 EDT Jul 10 -- ET date is Jul 10.
        assert lifecycle_phase(now_utc, event) == EventLifecyclePhase.ACTIVE


class TestResolveHomeOwner:
    """Single-owner-by-priority + tie-break behavior."""

    def test_unopposed_active_event_owns_home(self) -> None:
        sl = _sl_like_event((date(2026, 7, 9),), priority=100)
        owner = resolve_home_owner(datetime(2026, 7, 9, 18, 0), (sl,))
        assert owner is sl

    def test_no_home_eligible_event_returns_none(self) -> None:
        sl = _sl_like_event((date(2026, 7, 9),), priority=100)
        # Far from the window -- Dormant, not home-eligible.
        owner = resolve_home_owner(datetime(2026, 1, 1, 12, 0), (sl,))
        assert owner is None

    def test_higher_priority_wins_when_both_home_eligible(self) -> None:
        march_madness = DeskEvent(
            key="march_madness", priority=100, window_priors=_PRIORS, game_dates=(date(2026, 7, 9),)
        )
        summer_league = DeskEvent(
            key="summer_league", priority=80, window_priors=_PRIORS, game_dates=(date(2026, 7, 9),)
        )
        owner = resolve_home_owner(datetime(2026, 7, 9, 18, 0), (summer_league, march_madness))
        assert owner is march_madness

    def test_active_beats_warmup_when_priority_ties(self) -> None:
        # Both priority=80; one is Active today, the other only Warm-up.
        active_event = DeskEvent(
            key="a", priority=80, window_priors=_PRIORS, game_dates=(date(2026, 7, 9),)
        )
        warmup_event = DeskEvent(
            key="b", priority=80, window_priors=_PRIORS, game_dates=(date(2026, 7, 11),)
        )
        owner = resolve_home_owner(datetime(2026, 7, 9, 18, 0), (warmup_event, active_event))
        assert owner is active_event
