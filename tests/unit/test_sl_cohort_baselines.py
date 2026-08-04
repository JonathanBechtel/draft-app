"""Unit tests for the Summer League Desk cohort-baseline builder (Job A, #502).

Pure-math and pure-classification coverage: the window-rule buckets, the
cohort_key/cohort_kind string contracts downstream tickets read, the
percentile-breakpoint math, and the min-minutes gate. No DB — see
``tests/integration/test_sl_cohort_baselines.py`` for the end-to-end build.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from app.schemas.summer_league_desk import (
    SummerLeagueDeskCohortKind,
    SummerLeagueDeskGrain,
)
from app.services.sources.summer_league.cohort_baselines import (
    DEFAULT_GAME_MIN_MINUTES,
    DEFAULT_MIN_MINUTES,
    EventAggregate,
    FirstQualifyingGame,
    blend_event_aggregates,
    cohort_key_for,
    cohort_kind_for,
    compute_breakpoints,
    compute_mean,
    compute_median,
    first_qualifying_games,
    qualifying_game_values,
    slot_bounds_for,
    slot_window,
)
from app.services.sources.summer_league.desk_grades import percentile_of_value
from app.services.sources.summer_league.metrics import game_score_line


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

    def test_game_prefix_mirrors_slot_suffix(self) -> None:
        assert cohort_key_for(1, 1, grain=SummerLeagueDeskGrain.GAME) == "game:1-4"

    def test_game_prefix_mirrors_round_suffix(self) -> None:
        assert cohort_key_for(1, 20, grain=SummerLeagueDeskGrain.GAME) == "game:1_late"
        assert cohort_key_for(2, 5, grain=SummerLeagueDeskGrain.GAME) == "game:2"

    def test_game_prefix_mirrors_undrafted_suffix(self) -> None:
        assert (
            cohort_key_for(None, None, grain=SummerLeagueDeskGrain.GAME)
            == "game:undrafted"
        )

    def test_game_prefix_never_collides_with_event_prefix(self) -> None:
        """The uniqueness this key format exists to preserve (module docstring).

        A T1 row is unique on (baseline_version, cohort_key) -- game and event
        grain must never produce the same string for the same slot.
        """
        assert cohort_key_for(1, 1, grain=SummerLeagueDeskGrain.GAME) != cohort_key_for(
            1, 1, grain=SummerLeagueDeskGrain.EVENT
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


# --------------------------------------------------------------------------- #
# Game-grain: individual per-game GmSc values, per-game min-minutes gate (#525)
# --------------------------------------------------------------------------- #
def _game_row(
    player_id: int, game_id: int, minutes_seconds: int, pts: float = 10.0
) -> SimpleNamespace:
    """A minimal box line -- other `game_score_line` fields default to 0 via
    `game_score_from_row`'s `getattr(row, f, 0)`, so with every other
    component at 0 the resulting GmSc equals `pts` exactly."""
    return SimpleNamespace(
        player_id=player_id, game_id=game_id, minutes_seconds=minutes_seconds, pts=pts
    )


def test_qualifying_game_values_gates_below_floor_per_game() -> None:
    """A single game under the per-game minutes floor is dropped entirely."""
    rows = [
        _game_row(1, 100, minutes_seconds=5 * 60, pts=20.0),  # 5 min, below floor
        _game_row(1, 101, minutes_seconds=15 * 60, pts=12.0),  # 15 min, clears
    ]
    out = qualifying_game_values(rows, min_minutes=10.0)
    game_ids = {gv.game_id for gv in out}
    assert game_ids == {101}


def test_qualifying_game_values_keeps_every_qualifying_game_ungrouped() -> None:
    """Unlike blend_event_aggregates, each qualifying game is its own data point."""
    rows = [
        _game_row(1, 100, minutes_seconds=20 * 60, pts=10.0),
        _game_row(1, 101, minutes_seconds=20 * 60, pts=30.0),
    ]
    out = qualifying_game_values(rows, min_minutes=10.0)
    assert len(out) == 2
    assert {gv.gmsc for gv in out} == {10.0, 30.0}
    assert all(gv.player_id == 1 for gv in out)


def test_qualifying_game_values_computes_gmsc_via_game_score_from_row() -> None:
    """GmSc is the real Hollinger Game Score, not the raw pts total."""
    row = SimpleNamespace(
        player_id=1,
        game_id=100,
        minutes_seconds=25 * 60,
        pts=20,
        fgm=8,
        fga=15,
        ftm=4,
        fta=5,
        oreb=1,
        dreb=4,
        ast=3,
        stl=2,
        blk=1,
        tov=2,
        pf=3,
    )
    out = qualifying_game_values([row], min_minutes=10.0)
    assert len(out) == 1
    expected = game_score_line(
        pts=20,
        fgm=8,
        fga=15,
        ftm=4,
        fta=5,
        oreb=1,
        dreb=4,
        ast=3,
        stl=2,
        blk=1,
        tov=2,
        pf=3,
    )
    assert out[0].gmsc == round(expected, 2)


def test_qualifying_game_values_empty_rows_returns_empty_list() -> None:
    assert qualifying_game_values([], min_minutes=10.0) == []


def test_qualifying_game_values_default_floor_is_lower_than_event_floor() -> None:
    """The per-game floor (#525) is deliberately much lower than the event grain's."""
    assert DEFAULT_GAME_MIN_MINUTES < DEFAULT_MIN_MINUTES


# --------------------------------------------------------------------------- #
# The bug this ticket fixes: event-grain vs game-grain percentiles diverge
# --------------------------------------------------------------------------- #
def test_same_game_ranks_differently_against_event_grain_vs_game_grain() -> None:
    """A hand-built cohort with known single-game spread: the two grains disagree.

    Event-aggregate GmSc (season-blended, low variance) and individual-game
    GmSc (high variance) are different distributions even when they come from
    the same underlying players -- ranking one game against the wrong one
    (the bug #525 fixes) skews the percentile toward the tails.
    """
    # Event grain: 5 players' season-blended GmSc -- tightly clustered.
    event_values = [14.0, 15.0, 16.0, 17.0, 18.0]
    # Game grain: the same players' individual games -- much wider spread
    # (a hot night can far exceed, a cold night can far undercut, the season
    # average).
    game_values = [2.0, 8.0, 15.0, 24.0, 30.0, 6.0, 20.0, 12.0, 28.0, 4.0]

    event_breakpoints = compute_breakpoints(event_values)
    game_breakpoints = compute_breakpoints(game_values)

    # One big game: 22 GmSc.
    subject_game_gmsc = 22.0
    event_pctl = percentile_of_value(event_breakpoints, subject_game_gmsc)
    game_pctl = percentile_of_value(game_breakpoints, subject_game_gmsc)

    assert event_pctl != game_pctl
    # Ranked against the tight event distribution, 22 reads near the very
    # top (inflated); against the true wide game distribution it's real but
    # more modest.
    assert event_pctl > game_pctl


# --------------------------------------------------------------------------- #
# first_qualifying_games (#539): the ONE shared debut-game reduction
# --------------------------------------------------------------------------- #
def _dated_row(
    player_id: int,
    game_id: int,
    minutes_seconds: int,
    pts: float = 10.0,
) -> SimpleNamespace:
    """A minimal box line -- GmSc equals ``pts`` (every other component 0)."""
    return SimpleNamespace(
        player_id=player_id, game_id=game_id, minutes_seconds=minutes_seconds, pts=pts
    )


def test_first_qualifying_games_hand_computed_picks_earliest_qualifying_per_player() -> (
    None
):
    """Hand-computed: each player's earliest-dated qualifying game wins, not the highest GmSc."""
    rows = [
        # Player 1: game 102 (July 5, 30 GmSc) precedes game 101 (July 10, 10
        # GmSc) chronologically -- the EARLIER date wins even though it's the
        # lower-GmSc game.
        (_dated_row(1, 101, 20 * 60, pts=10.0), date(2024, 7, 10)),
        (_dated_row(1, 102, 20 * 60, pts=30.0), date(2024, 7, 5)),
        # Player 2: a single qualifying game.
        (_dated_row(2, 200, 20 * 60, pts=18.0), date(2024, 7, 8)),
    ]
    result = first_qualifying_games(rows, min_minutes=10.0)

    assert result[1] == FirstQualifyingGame(
        player_id=1, game_id=102, gmsc=30.0, game_date=date(2024, 7, 5)
    )
    assert result[2] == FirstQualifyingGame(
        player_id=2, game_id=200, gmsc=18.0, game_date=date(2024, 7, 8)
    )


def test_first_qualifying_games_gates_below_floor_even_if_chronologically_first() -> (
    None
):
    """A thin game that's chronologically first is skipped for the next qualifying one."""
    rows = [
        (_dated_row(1, 100, 3 * 60, pts=40.0), date(2024, 7, 1)),  # 3 min -- dropped
        (
            _dated_row(1, 101, 15 * 60, pts=12.0),
            date(2024, 7, 5),
        ),  # 15 min -- qualifies
    ]
    result = first_qualifying_games(rows, min_minutes=10.0)
    assert result[1].game_id == 101
    assert result[1].gmsc == 12.0


def test_first_qualifying_games_ties_break_on_lower_game_id() -> None:
    """A same-day doubleheader breaks the tie on the lower game_id, deterministically."""
    rows = [
        (_dated_row(1, 202, 20 * 60, pts=5.0), date(2024, 7, 10)),
        (_dated_row(1, 201, 20 * 60, pts=9.0), date(2024, 7, 10)),
    ]
    result = first_qualifying_games(rows, min_minutes=10.0)
    assert result[1].game_id == 201


def test_first_qualifying_games_missing_date_never_wins_over_a_dated_row() -> None:
    """A row with no game_date sorts last -- it never counts as "first"."""
    rows = [
        (_dated_row(1, 100, 20 * 60, pts=5.0), None),
        (_dated_row(1, 101, 20 * 60, pts=9.0), date(2024, 7, 10)),
    ]
    result = first_qualifying_games(rows, min_minutes=10.0)
    assert result[1].game_id == 101


def test_first_qualifying_games_empty_rows_returns_empty_dict() -> None:
    assert first_qualifying_games([], min_minutes=10.0) == {}


def test_first_qualifying_games_skips_rows_with_no_player_id() -> None:
    row = SimpleNamespace(player_id=None, game_id=1, minutes_seconds=20 * 60, pts=10.0)
    assert first_qualifying_games([(row, date(2024, 7, 10))], min_minutes=10.0) == {}


def test_first_qualifying_games_computes_gmsc_via_game_score_from_row() -> None:
    """GmSc is the real Hollinger Game Score, not the raw pts total."""
    row = SimpleNamespace(
        player_id=1,
        game_id=100,
        minutes_seconds=25 * 60,
        pts=20,
        fgm=8,
        fga=15,
        ftm=4,
        fta=5,
        oreb=1,
        dreb=4,
        ast=3,
        stl=2,
        blk=1,
        tov=2,
        pf=3,
    )
    result = first_qualifying_games([(row, date(2024, 7, 10))], min_minutes=10.0)
    expected = game_score_line(
        pts=20,
        fgm=8,
        fga=15,
        ftm=4,
        fta=5,
        oreb=1,
        dreb=4,
        ast=3,
        stl=2,
        blk=1,
        tov=2,
        pf=3,
    )
    assert result[1].gmsc == round(expected, 2)
