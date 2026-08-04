"""Unit tests for the Summer League Desk commentary selection layer (#518).

Pure-logic coverage only -- no DB (Stage 2 is fed already-detected Facts from
Stage 1, #520). Covers: notability-descending ordering, the notability floor
(suppressed from prose but never deleted from the Fact list), per-surface
top-k, the general subsumption/dedup rule (rank #1 beats its own
percentile), and deterministic tie-breaking.
"""

from __future__ import annotations

import pytest

from app.schemas.summer_league_desk import SummerLeagueDeskGrade
from app.services.sources.summer_league.desk_facts import (
    CohortPeer,
    Fact,
    FactKind,
    FactProvenance,
    FactSubject,
    detect_cohort_rank,
    detect_percentile,
)
from app.services.sources.summer_league.desk_grades import GradeRow
from app.services.sources.summer_league.desk_selection import (
    NOTABILITY_FLOOR,
    SURFACE_K,
    Surface,
    chip_facts,
    dedup_facts,
    meets_notability_floor,
    select_facts,
)

SUBJECT = FactSubject(player_id=1, player_label="Test Prospect", competition_id=10)


def _fact(
    *,
    kind: FactKind = FactKind.SELF_DELTA,
    subject: FactSubject = SUBJECT,
    metric: str = "gmsc",
    cohort: str | None = "slot:1-4",
    notability: float = 0.5,
    values: dict[str, object] | None = None,
) -> Fact:
    """Build a minimal Fact directly -- selection logic only cares about
    kind/subject/metric/cohort/notability (+ ``values["rank"]`` for the
    cohort_rank subsumption condition)."""
    return Fact(
        kind=kind,
        subject=subject,
        metric=metric,
        cohort=cohort,
        values=values if values is not None else {},
        notability=notability,
        provenance=FactProvenance(detector_id=kind.value),
    )


# --------------------------------------------------------------------------- #
# dedup_facts / subsumption
# --------------------------------------------------------------------------- #
def test_dedup_rank_one_subsumes_its_own_percentile() -> None:
    """The spec's pinned example: a #1 cohort rank beats its own percentile
    Fact on the same subject/metric/cohort -- only the rank Fact survives."""
    rank_fact = detect_cohort_rank(
        subject=SUBJECT,
        subject_value=25.0,
        metric="gmsc",
        cohort_key="slot:1-4",
        peers=[CohortPeer(label="Peer A", value=18.9)],
    )
    assert rank_fact is not None
    assert rank_fact.values["rank"] == 1  # sanity: this IS the rank-1 case

    grade = GradeRow(
        player_id=SUBJECT.player_id,
        competition_id=10,
        baseline_version="v1",
        cohort_key="slot:1-4",
        subject_value=25.0,
        pctl=96.0,
        grade=SummerLeagueDeskGrade.HOT,
        n_cohort=40,
        gated=False,
    )
    pctl_fact = detect_percentile(subject=SUBJECT, grade=grade, metric="gmsc")

    # Rank #1 saturates notability at 1.0; a 96th-pctl grade scores high but
    # strictly below that -- confirms which one should win before dedup runs.
    assert rank_fact.notability > pctl_fact.notability

    survivors = dedup_facts([pctl_fact, rank_fact])
    assert survivors == [rank_fact]


def test_dedup_keeps_facts_on_different_axes() -> None:
    """Facts on a different metric/cohort never collide -- both survive."""
    a = _fact(kind=FactKind.STREAK, metric="gmsc", cohort="slot:1-4", notability=0.7)
    b = _fact(
        kind=FactKind.DEBUT_VS_BAR, metric="ast", cohort="debut:1-4", notability=0.6
    )
    survivors = dedup_facts([a, b])
    assert len(survivors) == 2
    assert a in survivors
    assert b in survivors


def test_dedup_keeps_facts_for_different_subjects() -> None:
    """Same metric/cohort but different players is not an overlap."""
    other = FactSubject(player_id=2, player_label="Other Prospect", competition_id=10)
    a = _fact(subject=SUBJECT, notability=0.6)
    b = _fact(subject=other, notability=0.6)
    survivors = dedup_facts([a, b])
    assert len(survivors) == 2
    assert a in survivors
    assert b in survivors


def test_dedup_never_deletes_from_original_list() -> None:
    """dedup_facts returns a new list -- the caller's input is untouched."""
    a = _fact(kind=FactKind.STREAK, notability=0.9)
    b = _fact(kind=FactKind.PERCENTILE, notability=0.3)
    original = [a, b]
    dedup_facts(original)
    assert original == [a, b]


def test_dedup_keeps_streak_and_percentile_on_same_axis() -> None:
    """A streak and a percentile on the SAME metric+cohort are different
    claims (an active run vs. an event-aggregate standing) -- neither
    subsumes the other, so BOTH survive. This is the regression the
    axis-wide collapse got wrong: the streak was silently swallowed."""
    pctl = _fact(
        kind=FactKind.PERCENTILE, metric="gmsc", cohort="slot:1-4", notability=0.9
    )
    streak = _fact(
        kind=FactKind.STREAK, metric="gmsc", cohort="slot:1-4", notability=0.7
    )
    survivors = dedup_facts([pctl, streak])
    assert len(survivors) == 2
    assert pctl in survivors
    assert streak in survivors


def test_dedup_rank_seven_subsumes_nothing() -> None:
    """A cohort_rank that is NOT rank 1 restates no percentile -- the
    strength condition (rank==1) fails, so both survive even sharing an
    axis. Encoded via the condition, not inferred from notability order."""
    rank7 = _fact(
        kind=FactKind.COHORT_RANK,
        metric="gmsc",
        cohort="slot:1-4",
        notability=0.4,
        values={"rank": 7},
    )
    pctl = _fact(
        kind=FactKind.PERCENTILE, metric="gmsc", cohort="slot:1-4", notability=0.9
    )
    survivors = dedup_facts([rank7, pctl])
    assert len(survivors) == 2
    assert rank7 in survivors
    assert pctl in survivors


def test_dedup_collapses_exact_duplicate_kind_on_same_axis() -> None:
    """Two Facts of the SAME kind on the same axis are one claim twice --
    keep only the more notable one."""
    hi = _fact(
        kind=FactKind.PERCENTILE, metric="gmsc", cohort="slot:1-4", notability=0.9
    )
    lo = _fact(
        kind=FactKind.PERCENTILE, metric="gmsc", cohort="slot:1-4", notability=0.4
    )
    survivors = dedup_facts([lo, hi])
    assert survivors == [hi]


# --------------------------------------------------------------------------- #
# notability floor
# --------------------------------------------------------------------------- #
def test_meets_notability_floor() -> None:
    hot = _fact(notability=NOTABILITY_FLOOR + 0.01)
    cold = _fact(notability=NOTABILITY_FLOOR - 0.01)
    assert meets_notability_floor(hot) is True
    assert meets_notability_floor(cold) is False
    # Exactly at the floor counts as meeting it.
    assert meets_notability_floor(_fact(notability=NOTABILITY_FLOOR)) is True


def test_select_facts_suppresses_low_notability_but_keeps_it_available_as_a_chip() -> (
    None
):
    """A stale/low-notability fact is dropped from prose selection but is
    never deleted -- it's still present in the caller's list and still
    renders via chip_facts (spec: "chips render regardless")."""
    weak = _fact(kind=FactKind.SELF_DELTA, metric="ast", notability=0.1)
    strong = _fact(kind=FactKind.STREAK, metric="gmsc", notability=0.9)
    facts = [weak, strong]

    prose = select_facts(facts, surface=Surface.HERO_TAGLINE)
    assert prose == [strong]
    assert weak not in prose

    # Chips ignore the floor and selection entirely.
    chips = chip_facts(facts)
    assert weak in chips
    assert strong in chips
    assert chips == facts
    assert chips is not facts  # new list, not the same object


def test_select_facts_returns_empty_when_everything_is_below_floor() -> None:
    facts = [_fact(notability=0.1), _fact(kind=FactKind.STREAK, notability=0.2)]
    assert select_facts(facts, surface=Surface.HERO_TAGLINE) == []


# --------------------------------------------------------------------------- #
# surface policies (k)
# --------------------------------------------------------------------------- #
def test_hero_tagline_selects_exactly_one() -> None:
    facts = [
        _fact(kind=FactKind.STREAK, metric="gmsc", notability=0.9),
        _fact(kind=FactKind.SELF_DELTA, metric="ast", notability=0.85),
        _fact(kind=FactKind.DEBUT_VS_BAR, metric="tov_pct", notability=0.8),
    ]
    assert SURFACE_K[Surface.HERO_TAGLINE] == 1
    selected = select_facts(facts, surface=Surface.HERO_TAGLINE)
    assert len(selected) == 1
    assert selected[0].notability == 0.9


def test_tick_note_selects_up_to_configured_k() -> None:
    facts = [
        _fact(kind=FactKind.STREAK, metric="gmsc", notability=0.95),
        _fact(kind=FactKind.SELF_DELTA, metric="ast", notability=0.9),
        _fact(kind=FactKind.DEBUT_VS_BAR, metric="tov_pct", notability=0.85),
        _fact(kind=FactKind.COUNT_CLUB, metric="reb", notability=0.8),
        _fact(kind=FactKind.FIRST_SINCE, metric="stl", notability=0.75),
    ]
    k = SURFACE_K[Surface.TICK_NOTE]
    selected = select_facts(facts, surface=Surface.TICK_NOTE)
    assert len(selected) == k
    assert [f.notability for f in selected] == sorted(
        (f.notability for f in facts), reverse=True
    )[:k]


def test_tick_note_k_is_reachable_for_one_subject_same_axis() -> None:
    """The bug the axis-wide collapse caused: a single player with four
    distinct, non-overlapping kinds on ONE metric+cohort must be able to
    fill k=3 -- previously all four collapsed to one and k>1 was
    unreachable for any single-subject surface."""
    facts = [
        _fact(
            kind=FactKind.PERCENTILE, metric="gmsc", cohort="slot:1-4", notability=0.9
        ),
        _fact(kind=FactKind.STREAK, metric="gmsc", cohort="slot:1-4", notability=0.85),
        _fact(
            kind=FactKind.SELF_DELTA, metric="gmsc", cohort="slot:1-4", notability=0.8
        ),
        _fact(
            kind=FactKind.COUNT_CLUB, metric="gmsc", cohort="slot:1-4", notability=0.75
        ),
    ]
    selected = select_facts(facts, surface=Surface.TICK_NOTE)
    assert len(selected) == SURFACE_K[Surface.TICK_NOTE] == 3


def test_ledger_echo_uses_its_own_surface_default() -> None:
    facts = [_fact(kind=FactKind.STREAK, notability=0.9)]
    selected = select_facts(facts, surface=Surface.LEDGER_ECHO)
    assert selected == facts


def test_k_override_wins_over_surface_default() -> None:
    facts = [
        _fact(kind=FactKind.STREAK, metric="gmsc", notability=0.9),
        _fact(kind=FactKind.SELF_DELTA, metric="ast", notability=0.8),
    ]
    selected = select_facts(facts, surface=Surface.HERO_TAGLINE, k=2)
    assert len(selected) == 2


@pytest.mark.parametrize("bad_k", [0, -1])
def test_invalid_k_raises(bad_k: int) -> None:
    with pytest.raises(ValueError):
        select_facts([], surface=Surface.HERO_TAGLINE, k=bad_k)


# --------------------------------------------------------------------------- #
# determinism / tie-breaking
# --------------------------------------------------------------------------- #
def test_ties_break_deterministically_regardless_of_input_order() -> None:
    """Two facts tied on notability must sort identically no matter what
    order they're passed in -- never left to dict/set iteration order."""
    a = _fact(kind=FactKind.STREAK, metric="gmsc", cohort="slot:1-4", notability=0.9)
    b = _fact(
        kind=FactKind.SELF_DELTA, metric="ast", cohort="slot:5-14", notability=0.9
    )
    forward = select_facts([a, b], surface=Surface.TICK_NOTE)
    backward = select_facts([b, a], surface=Surface.TICK_NOTE)
    assert forward == backward
    assert len(forward) == 2


def test_tie_break_is_stable_across_repeated_calls() -> None:
    a = _fact(kind=FactKind.COUNT_CLUB, metric="gmsc", notability=0.7)
    b = _fact(kind=FactKind.FIRST_SINCE, metric="gmsc", notability=0.7)
    results = {
        tuple(f.kind for f in select_facts([a, b], surface=Surface.TICK_NOTE))
        for _ in range(5)
    }
    assert len(results) == 1
