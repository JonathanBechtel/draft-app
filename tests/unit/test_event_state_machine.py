"""Table-driven unit tests for the inner Daily Coverage state machine (pure, no DB).

Covers: Live-always-wins, off-day Ledger persistence, the schedule-relative
Ledger->Morning flip boundary (both LEAD- and FLOOR-dominant cases), the
scheduled-tip fallback (clock past tip with stale statuses => Live), day rollover
across two separate game days, DST-correct Eastern floor conversion, the
off-window `None` contract, and postponed/canceled games as a terminal,
never-Live-triggering status (the #529/#530 follow-up: a postponed game's own past
tip must never strand the day in Live, nor keep a mixed day from reaching Recap).
"""

from __future__ import annotations

from datetime import date, datetime

from app.schemas.event_desk import EventDailyState
from app.services.event_desk.registry import DeskEvent, GameStatus, WindowPriors
from app.services.event_desk.state_machine import inner_state
from app.services.event_desk.timeutils import eastern_floor_to_utc

_PRIORS = WindowPriors(
    announce_horizon_days=14,
    pre_roll_days=3,
    gap_bridge_days=4,
    post_roll_days=2,
    morning_lead_h=6.0,
    morning_floor_et="09:00",
)

# A bridged, multi-day SL-like Active window (mirrors test_event_lifecycle.py) so
# every scenario below is squarely inside Active without re-deriving the outer
# lifecycle math per test.
_GAME_DATES = (
    date(2026, 7, 5),
    date(2026, 7, 9),
    date(2026, 7, 10),
    date(2026, 7, 12),
    date(2026, 7, 14),
    date(2026, 7, 16),
    date(2026, 7, 18),
    date(2026, 7, 20),
)


def _event() -> DeskEvent:
    return DeskEvent(
        key="summer_league", priority=100, window_priors=_PRIORS, game_dates=_GAME_DATES
    )


class TestLiveAlwaysWins:
    def test_any_in_progress_is_live_regardless_of_clock(self) -> None:
        event = _event()
        # Before the flip, no scheduled-tip fallback would fire on its own -- but
        # Live always wins per behavior spec §2.
        now = datetime(2026, 7, 9, 8, 0)
        schedule = (datetime(2026, 7, 9, 23, 0),)
        statuses = (GameStatus.IN_PROGRESS,)
        assert inner_state(now, schedule, statuses, event) == EventDailyState.LIVE


class TestOffDayPersistence:
    def test_no_games_today_persists_recap(self) -> None:
        """Jul 11 is a bridged gap day (no game, still Active) -- off-day => Ledger
        persists all day (behavior spec §2), regardless of `now`'s time of day."""
        event = _event()
        for hour in (0, 9, 13, 23):
            now = datetime(2026, 7, 11, hour, 0)
            assert inner_state(now, (), (), event) == EventDailyState.RECAP


class TestFlipBoundary:
    """LEAD (`first_tip - 6h`) vs FLOOR (`09:00 ET`) -- `max()` of the two."""

    def test_lead_based_flip_dominates_late_game(self) -> None:
        """Evening tip (19:00 ET / 23:00 UTC): LEAD puts the flip at 13:00 ET,
        later than the 09:00 ET floor -- LEAD wins."""
        event = _event()
        first_tip = datetime(2026, 7, 9, 23, 0)  # 19:00 EDT
        before = datetime(2026, 7, 9, 16, 59)
        at = datetime(2026, 7, 9, 17, 0)  # 13:00 EDT == first_tip - 6h

        assert inner_state(before, (first_tip,), (GameStatus.SCHEDULED,), event) == (
            EventDailyState.RECAP
        )
        assert inner_state(at, (first_tip,), (GameStatus.SCHEDULED,), event) == (
            EventDailyState.PREVIEW
        )

    def test_floor_dominates_early_game(self) -> None:
        """Late-morning tip (10:00 ET / 14:00 UTC): LEAD would put the flip at
        04:00 ET, but the 09:00 ET floor is later -- floor wins."""
        event = _event()
        first_tip = datetime(2026, 7, 9, 14, 0)  # 10:00 EDT
        floor_utc = eastern_floor_to_utc(first_tip, "09:00")
        assert floor_utc == datetime(2026, 7, 9, 13, 0)  # 09:00 EDT == UTC-4

        before = floor_utc.replace(minute=59, hour=floor_utc.hour - 1)
        at = floor_utc

        assert inner_state(before, (first_tip,), (GameStatus.SCHEDULED,), event) == (
            EventDailyState.RECAP
        )
        assert inner_state(at, (first_tip,), (GameStatus.SCHEDULED,), event) == (
            EventDailyState.PREVIEW
        )


class TestScheduledTipFallback:
    def test_stale_statuses_past_first_tip_still_render_live(self) -> None:
        """`now >= today's first tip` and not everything is final => Live, even
        when no game is *marked* in_progress (stale tick) -- behavior spec §2."""
        event = _event()
        first_tip = datetime(2026, 7, 9, 19, 0)
        now = datetime(2026, 7, 9, 19, 5)  # just past tip
        statuses = (GameStatus.SCHEDULED,)  # stale: feed hasn't caught up yet
        assert inner_state(now, (first_tip,), statuses, event) == EventDailyState.LIVE

    def test_all_final_after_first_tip_is_recap_not_fallback(self) -> None:
        """Once every known game is final, the day's last-final Recap wins over the
        fallback (which only exists to cover the gap while games are still live)."""
        event = _event()
        first_tip = datetime(2026, 7, 9, 19, 0)
        now = datetime(2026, 7, 9, 22, 0)
        statuses = (GameStatus.FINAL,)
        assert inner_state(now, (first_tip,), statuses, event) == EventDailyState.RECAP


class TestPostponedIsTerminalAndNeverLiveTriggering:
    """#529/#530 follow-up: postponed/canceled is a terminal, non-Live status.

    Per the provider contract (`registry.calendar_facts_for_competition_ids`), a
    postponed/canceled game's tip is withheld from `schedule` entirely -- only its
    terminal `GameStatus.POSTPONED` flows through in `statuses`. These tests exercise
    `inner_state` against exactly that (already-filtered) shape of input, proving the
    resolver itself never lets a postponed game keep a day Live or unresolved.
    """

    def test_postponed_only_day_is_recap_not_live_regardless_of_original_tip(
        self,
    ) -> None:
        """The core regression: a day whose only game is postponed must read as an
        off-day (Recap persists), never Live -- even hours after that game's
        original (now-irrelevant) tip would have passed."""
        event = _event()
        for hour in (0, 9, 20, 23):
            now = datetime(2026, 7, 9, hour, 0)
            assert inner_state(now, (), (GameStatus.POSTPONED,), event) == (
                EventDailyState.RECAP
            )

    def test_mixed_day_stays_preview_then_live_then_recap_around_postponed_game(
        self,
    ) -> None:
        """One real game (evening tip) plus one postponed game (earlier, already-
        past original tip, withheld from `schedule`): the day stays Preview right up
        to the real tip -- never flips Live off the postponed game's own tip -- goes
        Live at the real tip, then reaches Recap once the real game finals (the
        postponed game staying postponed forever)."""
        event = _event()
        real_tip = datetime(2026, 7, 9, 23, 0)  # 19:00 EDT
        # `schedule` only ever carries the real game's tip -- the postponed game's
        # (earlier) tip is never in it, per the provider contract.
        schedule = (real_tip,)

        # After the LEAD-dominant flip (real_tip - 6h = 17:00 UTC) but hours before
        # the real tip, and well after where the postponed game's own tip would have
        # been -- must stay Preview, not jump to Live.
        still_preview_at = datetime(2026, 7, 9, 18, 0)
        pre_statuses = (GameStatus.SCHEDULED, GameStatus.POSTPONED)
        assert inner_state(still_preview_at, schedule, pre_statuses, event) == (
            EventDailyState.PREVIEW
        )

        assert inner_state(real_tip, schedule, pre_statuses, event) == (
            EventDailyState.LIVE
        )

        after_final = datetime(2026, 7, 10, 2, 0)
        post_statuses = (GameStatus.FINAL, GameStatus.POSTPONED)
        assert inner_state(after_final, schedule, post_statuses, event) == (
            EventDailyState.RECAP
        )

    def test_in_progress_real_game_with_postponed_sibling_still_live(self) -> None:
        """Rule 1 (Live always wins) stays intact with a postponed game present
        alongside a genuinely live one."""
        event = _event()
        now = datetime(2026, 7, 9, 8, 0)
        schedule = (datetime(2026, 7, 9, 23, 0),)
        statuses = (GameStatus.IN_PROGRESS, GameStatus.POSTPONED)
        assert inner_state(now, schedule, statuses, event) == EventDailyState.LIVE


class TestDayRollover:
    def test_two_separate_game_days_each_resolve_independently(self) -> None:
        """The resolver is pure/stateless -- Jul 9's outcome must not leak into Jul
        10's flip math. Jul 9: last final -> Recap. Jul 10, pre-flip: still Recap
        (persisting). Jul 10, post-flip, pre-tip: Preview."""
        event = _event()

        jul9_final = datetime(2026, 7, 9, 22, 0)
        assert inner_state(
            jul9_final, (datetime(2026, 7, 9, 19, 0),), (GameStatus.FINAL,), event
        ) == EventDailyState.RECAP

        jul10_first_tip = datetime(2026, 7, 10, 19, 0)
        jul10_flip = eastern_floor_to_utc(jul10_first_tip, "09:00")

        pre_flip = jul10_flip.replace(hour=jul10_flip.hour - 1)
        assert inner_state(
            pre_flip, (jul10_first_tip,), (GameStatus.SCHEDULED,), event
        ) == EventDailyState.RECAP

        post_flip = jul10_flip
        assert inner_state(
            post_flip, (jul10_first_tip,), (GameStatus.SCHEDULED,), event
        ) == EventDailyState.PREVIEW


class TestOffWindowReturnsNone:
    def test_dormant_phase_returns_none(self) -> None:
        event = _event()
        now = datetime(2026, 1, 1, 12, 0)  # far outside the SL window
        assert inner_state(now, (), (), event) is None


class TestEasternFloorIsDstSafe:
    """`eastern_floor_to_utc` resolves the correct Eastern offset per-date -- a
    hardcoded UTC delta would be wrong on one side of the DST boundary."""

    def test_summer_is_edt_utc_minus_4(self) -> None:
        reference = datetime(2026, 7, 9, 12, 0)
        assert eastern_floor_to_utc(reference, "09:00") == datetime(2026, 7, 9, 13, 0)

    def test_winter_is_est_utc_minus_5(self) -> None:
        reference = datetime(2026, 1, 15, 12, 0)
        assert eastern_floor_to_utc(reference, "09:00") == datetime(2026, 1, 15, 14, 0)
