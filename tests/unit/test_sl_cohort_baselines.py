"""Unit tests for the Summer League Desk cohort-baseline builder (Job A, #502).

Pure-math and pure-classification coverage: the window-rule buckets, the
cohort_key/cohort_kind string contracts downstream tickets read, the
percentile-breakpoint math, and the min-minutes gate. No DB — see
``tests/integration/test_sl_cohort_baselines.py`` for the end-to-end build.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.schemas.summer_league_desk import (
    SummerLeagueDeskCohortKind,
    SummerLeagueDeskGrain,
)
from app.services.summer_league.cohort_baselines import (
    EventAggregate,
    blend_event_aggregates,
    cohort_key_for,
    cohort_kind_for,
    compute_breakpoints,
    compute_mean,
    compute_median,
    slot_bounds_for,
    slot_window,
)


# --------------------------------------------------------------------------- #
# Window rule
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "pick,expected",
    [
        (1, (1, 4)),
        (2, (1, 5)),
        (5, (2, 8)),
        (7, (4, 10)),
        (11, (8, 14)),
        (12, (9, 14)),
        (14, (11, 14)),
    ],
)
def test_slot_window_clamps_at_edges(pick: int, expected: tuple[int, int]) -> None:
    """The ±3 lottery window clamps to [1, 14] without re-centering."""
    assert slot_window(pick) == expected


class TestCohortKindFor:
    """Classification must key off draft_round + draft_pick, never pick alone."""

    def test_round1_pick_within_14_is_lottery(self) -> None:
        assert cohort_kind_for(1, 4) == SummerLeagueDeskCohortKind.SLOT_WINDOW

    def test_round2_pick_within_14_is_not_lottery(self) -> None:
        """The domain gotcha this ticket exists to guard against.

        draft_pick is WITHIN-ROUND: a round-2 pick of 4 is overall #34, not a
        lottery pick — testing draft_pick alone would wrongly sweep this in.
        """
        assert cohort_kind_for(2, 4) == SummerLeagueDeskCohortKind.ROUND_BUCKET

    def test_round1_pick_15_is_round_bucket_not_lottery(self) -> None:
        assert cohort_kind_for(1, 15) == SummerLeagueDeskCohortKind.ROUND_BUCKET

    def test_round1_pick_30_is_round_bucket(self) -> None:
        assert cohort_kind_for(1, 30) == SummerLeagueDeskCohortKind.ROUND_BUCKET

    def test_round2_any_pick_is_round_bucket(self) -> None:
        assert cohort_kind_for(2, 30) == SummerLeagueDeskCohortKind.ROUND_BUCKET

    def test_no_draft_round_is_status(self) -> None:
        assert cohort_kind_for(None, None) == SummerLeagueDeskCohortKind.STATUS

    def test_round_without_pick_is_status(self) -> None:
        """Defensive: a round with no pick number is treated as undrafted."""
        assert cohort_kind_for(1, None) == SummerLeagueDeskCohortKind.STATUS

    def test_debut_grain_always_returns_debut_kind(self) -> None:
        """cohort_kind=debut regardless of the underlying slot/round/status."""
        assert (
            cohort_kind_for(1, 1, grain=SummerLeagueDeskGrain.DEBUT)
            == SummerLeagueDeskCohortKind.DEBUT
        )
        assert (
            cohort_kind_for(None, None, grain=SummerLeagueDeskGrain.DEBUT)
            == SummerLeagueDeskCohortKind.DEBUT
        )


class TestCohortKeyFor:
    """The exact cohort_key string format downstream #503/#504/#520 must match."""

    def test_lottery_pick_1(self) -> None:
        assert cohort_key_for(1, 1) == "slot:1-4"

    def test_lottery_pick_14(self) -> None:
        assert cohort_key_for(1, 14) == "slot:11-14"

    def test_round1_late(self) -> None:
        assert cohort_key_for(1, 20) == "round:1_late"

    def test_round2(self) -> None:
        assert cohort_key_for(2, 10) == "round:2"

    def test_undrafted(self) -> None:
        assert cohort_key_for(None, None) == "status:undrafted"

    def test_debut_prefix_mirrors_slot_suffix(self) -> None:
        assert cohort_key_for(1, 1, grain=SummerLeagueDeskGrain.DEBUT) == "debut:1-4"

    def test_debut_prefix_mirrors_round_suffix(self) -> None:
        assert (
            cohort_key_for(1, 20, grain=SummerLeagueDeskGrain.DEBUT) == "debut:1_late"
        )
        assert cohort_key_for(2, 5, grain=SummerLeagueDeskGrain.DEBUT) == "debut:2"

    def test_debut_prefix_mirrors_undrafted_suffix(self) -> None:
        assert (
            cohort_key_for(None, None, grain=SummerLeagueDeskGrain.DEBUT)
            == "debut:undrafted"
        )


class TestSlotBoundsFor:
    """slot_low/slot_high store human-facing OVERALL draft-position bounds."""

    def test_lottery_bounds_match_window(self) -> None:
        assert slot_bounds_for(1, 5) == (2, 8)

    def test_round1_late_bounds_are_overall_15_30(self) -> None:
        assert slot_bounds_for(1, 20) == (15, 30)

    def test_round2_bounds_are_overall_31_60(self) -> None:
        """Round 2's within-round picks 1-30 == overall picks 31-60."""
        assert slot_bounds_for(2, 1) == (31, 60)
        assert slot_bounds_for(2, 30) == (31, 60)

    def test_undrafted_has_no_bounds(self) -> None:
        assert slot_bounds_for(None, None) is None


# --------------------------------------------------------------------------- #
# Distribution math
# --------------------------------------------------------------------------- #
def test_compute_breakpoints_hand_computed() -> None:
    """A small, evenly-spaced distribution has exactly-computable breakpoints."""
    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    bp = compute_breakpoints(values, percentiles=(0, 25, 50, 75, 100))
    assert bp == {"0": 10.0, "25": 20.0, "50": 30.0, "75": 40.0, "100": 50.0}


def test_compute_breakpoints_interpolates_between_ranks() -> None:
    """p10 over [10, 20, 30, 40, 50] falls between rank 0 and rank 1."""
    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    bp = compute_breakpoints(values, percentiles=(10,))
    # rank = 0.10 * 4 = 0.4 -> 10 + 0.4*(20-10) = 14.0
    assert bp["10"] == 14.0


def test_compute_breakpoints_single_value() -> None:
    """A cohort with exactly one member: every percentile equals that value."""
    bp = compute_breakpoints([42.0], percentiles=(0, 50, 100))
    assert bp == {"0": 42.0, "50": 42.0, "100": 42.0}


def test_compute_breakpoints_empty_is_empty_dict() -> None:
    assert compute_breakpoints([]) == {}


def test_compute_mean_and_median_odd() -> None:
    values = [10.0, 20.0, 30.0]
    assert compute_mean(values) == 20.0
    assert compute_median(values) == 20.0


def test_compute_mean_and_median_even() -> None:
    values = [10.0, 20.0, 30.0, 40.0]
    assert compute_mean(values) == 25.0
    assert compute_median(values) == 25.0


def test_compute_mean_median_empty_is_zero() -> None:
    assert compute_mean([]) == 0.0
    assert compute_median([]) == 0.0


# --------------------------------------------------------------------------- #
# Event-aggregate blending + min-minutes gate
# --------------------------------------------------------------------------- #
def _row(player_id: int, year: int, gmsc: float | None, minutes: float, gp: int):
    return SimpleNamespace(
        player_id=player_id, year=year, gmsc=gmsc, minutes=minutes, gp=gp
    )


def test_blend_event_aggregates_blends_across_venues_games_weighted() -> None:
    """Two same-year rows for one player blend GmSc games-weighted (not summed)."""
    rows = [
        _row(1, 2024, gmsc=10.0, minutes=50.0, gp=2),
        _row(1, 2024, gmsc=20.0, minutes=50.0, gp=2),
    ]
    out = blend_event_aggregates(rows, min_minutes=40.0)
    agg = out[(1, 2024)]
    assert isinstance(agg, EventAggregate)
    # games-weighted mean: (10*2 + 20*2) / 4 == 15.0
    assert agg.gmsc == 15.0
    assert agg.minutes == 100.0
    assert agg.gp == 4


def test_blend_event_aggregates_min_minutes_gate_drops_below_floor() -> None:
    """An event whose blended minutes fall under the gate is excluded entirely."""
    rows = [
        _row(1, 2024, gmsc=10.0, minutes=39.9, gp=1),
        _row(2, 2024, gmsc=10.0, minutes=40.0, gp=1),
    ]
    out = blend_event_aggregates(rows, min_minutes=40.0)
    assert (1, 2024) not in out
    assert (2, 2024) in out


def test_blend_event_aggregates_separates_players_and_years() -> None:
    rows = [
        _row(1, 2023, gmsc=10.0, minutes=50.0, gp=1),
        _row(1, 2024, gmsc=20.0, minutes=50.0, gp=1),
        _row(2, 2024, gmsc=30.0, minutes=50.0, gp=1),
    ]
    out = blend_event_aggregates(rows, min_minutes=40.0)
    assert set(out.keys()) == {(1, 2023), (1, 2024), (2, 2024)}


def test_blend_event_aggregates_skips_null_gmsc_rows() -> None:
    """A row with no GmSc contributes minutes to the gate but no value weight."""
    rows = [_row(1, 2024, gmsc=None, minutes=50.0, gp=1)]
    out = blend_event_aggregates(rows, min_minutes=40.0)
    assert (1, 2024) not in out
