"""Unit tests for the Summer League Desk fact-query read layer's pure helpers (#524).

Pure-logic coverage only -- no DB. The async `fetch_*` functions in
`app.services.sources.summer_league.desk_fact_queries` are exercised end to end via
`tests/integration/test_sl_desk_fact_wiring.py`; this file covers the pure
slicing helpers built on top of a caller-supplied :class:`CohortMember` list
(:func:`cohort_peers`, :func:`club_members_clearing`,
:func:`most_recent_prior_holder`, :func:`count_club_threshold`) plus
:func:`field_peers`.
"""

from __future__ import annotations

from app.schemas.summer_league_desk import (
    SummerLeagueCohortBaseline,
    SummerLeagueDeskCohortKind,
    SummerLeagueDeskGrain,
)
from app.services.sources.summer_league.desk_fact_queries import (
    CohortMember,
    FieldEntry,
    club_members_clearing,
    cohort_peers,
    count_club_threshold,
    field_peers,
    most_recent_prior_holder,
)

MEMBERS = [
    CohortMember(player_id=1, player_label="Subject", year=2026, value=75.0),
    CohortMember(player_id=2, player_label="Peer2025", year=2025, value=18.9),
    CohortMember(player_id=3, player_label="Peer2019", year=2019, value=50.0),
    CohortMember(player_id=4, player_label="Peer2022", year=2022, value=45.0),
]


# --------------------------------------------------------------------------- #
# cohort_peers
# --------------------------------------------------------------------------- #
def test_cohort_peers_excludes_subject_by_player_id() -> None:
    peers = cohort_peers(MEMBERS, exclude_player_id=1)
    assert {p.label for p in peers} == {"Peer2025", "Peer2019", "Peer2022"}
    assert all(p.value != 75.0 for p in peers)


def test_cohort_peers_empty_when_no_members() -> None:
    assert cohort_peers([], exclude_player_id=1) == []


# --------------------------------------------------------------------------- #
# count_club_threshold
# --------------------------------------------------------------------------- #
def _baseline(**overrides: object) -> SummerLeagueCohortBaseline:
    defaults: dict[str, object] = dict(
        baseline_version="v1",
        is_active=True,
        cohort_key="slot:1-4",
        cohort_kind=SummerLeagueDeskCohortKind.SLOT_WINDOW,
        metric="gmsc",
        grain=SummerLeagueDeskGrain.EVENT,
        venue_scope="all",
        season_range="2017-2025",
        min_minutes=0.0,
        n_members=10,
        breakpoints={"0": 5.0, "50": 25.0, "90": 45.0, "100": 60.0},
        mean_value=25.0,
        median_value=25.0,
    )
    defaults.update(overrides)
    return SummerLeagueCohortBaseline(**defaults)  # type: ignore[arg-type]


def test_count_club_threshold_reads_the_90th_breakpoint() -> None:
    assert count_club_threshold(_baseline()) == 45.0


def test_count_club_threshold_none_when_breakpoint_missing() -> None:
    baseline = _baseline(breakpoints={"0": 5.0, "50": 25.0})
    assert count_club_threshold(baseline) is None


# --------------------------------------------------------------------------- #
# club_members_clearing
# --------------------------------------------------------------------------- #
def test_club_members_clearing_filters_by_threshold_and_excludes_subject() -> None:
    club = club_members_clearing(MEMBERS, exclude_player_id=1, threshold=45.0)
    assert {m.label for m in club} == {"Peer2019", "Peer2022"}


def test_club_members_clearing_lower_is_better() -> None:
    club = club_members_clearing(
        MEMBERS, exclude_player_id=1, threshold=20.0, higher_is_better=False
    )
    assert {m.label for m in club} == {"Peer2025"}


def test_club_members_clearing_empty_when_nobody_qualifies() -> None:
    assert club_members_clearing(MEMBERS, exclude_player_id=1, threshold=1000.0) == []


# --------------------------------------------------------------------------- #
# most_recent_prior_holder
# --------------------------------------------------------------------------- #
def test_most_recent_prior_holder_picks_the_latest_qualifying_year() -> None:
    holder = most_recent_prior_holder(
        MEMBERS, exclude_player_id=1, threshold=45.0, before_year=2026
    )
    assert holder is not None
    assert holder.label == "Peer2022"
    assert holder.year == 2022


def test_most_recent_prior_holder_none_when_nobody_qualifies() -> None:
    holder = most_recent_prior_holder(
        MEMBERS, exclude_player_id=1, threshold=1000.0, before_year=2026
    )
    assert holder is None


def test_most_recent_prior_holder_respects_before_year() -> None:
    # Peer2022 (year 2022) clears the threshold but is excluded by
    # `before_year=2020`; only Peer2019 (year 2019) remains eligible.
    holder = most_recent_prior_holder(
        MEMBERS, exclude_player_id=1, threshold=45.0, before_year=2020
    )
    assert holder is not None
    assert holder.label == "Peer2019"
    assert holder.year == 2019


# --------------------------------------------------------------------------- #
# field_peers
# --------------------------------------------------------------------------- #
def test_field_peers_excludes_subject() -> None:
    entries = [
        FieldEntry(player_id=1, player_label="Subject", value=32.0),
        FieldEntry(player_id=2, player_label="Bench", value=6.0),
    ]
    peers = field_peers(entries, exclude_player_id=1)
    assert len(peers) == 1
    assert peers[0].label == "Bench"
    assert peers[0].value == 6.0
