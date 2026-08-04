"""Unit tests for the Summer League Desk commentary realization layer (#519).

Pure-logic coverage only -- no DB (persistence is covered end to end in
``tests/integration/test_sl_desk_commentary.py``). Covers:

* Golden-string tests per template family (both curated variants, located via
  the module's own :func:`~app.services.sources.summer_league.desk_commentary._stable_variant_index`
  rather than guessed player ids).
* Cohort-key -> human-copy translation (:func:`humanize_cohort_key`), never
  leaking a raw ``cohort_key``.
* Determinism: double-render byte equality, and a shuffled-order matrix
  re-render that leaves every individual Fact's output unchanged.
* Gated percentiles hedge in prose but still chip.
* The banned-copy scan over **rendered output**, not source literals, across
  a broad matrix of Fact inputs.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest

from app.services.sources.summer_league.desk_commentary import (
    GRADE_PROSE_SURFACES,
    SLATE_HERO_PROSE_SURFACES,
    SLATE_PROSE_SURFACES,
    _stable_variant_index,
    build_facts_payload,
    humanize_cohort_key,
    render_chip,
    render_fact,
    render_prose_for_surface,
)
from app.services.sources.summer_league.desk_facts import (
    Fact,
    FactKind,
    FactProvenance,
    FactSubject,
)
from app.services.sources.summer_league.desk_selection import Surface


# --------------------------------------------------------------------------- #
# Fact-building helpers
# --------------------------------------------------------------------------- #
def _subject(pid: int, *, label: str = "Test Prospect") -> FactSubject:
    return FactSubject(player_id=pid, player_label=label, competition_id=1)


def _fact(
    *,
    kind: FactKind,
    pid: int,
    metric: str = "gmsc",
    cohort: Optional[str] = "slot:1-4",
    values: dict[str, Any],
    notability: float = 1.0,
) -> Fact:
    return Fact(
        kind=kind,
        subject=_subject(pid),
        metric=metric,
        cohort=cohort,
        values=values,
        notability=notability,
        provenance=FactProvenance(detector_id=kind.value),
    )


def _pid_for_variant(kind: FactKind, target: int, *, search_space: int = 500) -> int:
    """Find a player_id whose stable variant index (for `kind`) equals `target`.

    Discovers the mapping via the module's own pure hash function rather
    than a hardcoded guess, so the golden-string tests below stay correct
    even if the hash inputs change shape (as long as determinism itself
    holds -- which other tests in this file verify independently).
    """
    for pid in range(search_space):
        probe = _fact(kind=kind, pid=pid, values={})
        if _stable_variant_index(probe, 2) == target:
            return pid
    raise AssertionError(f"no player_id in range({search_space}) hits variant {target}")


# --------------------------------------------------------------------------- #
# humanize_cohort_key
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "cohort_key,expected",
    [
        (None, "his cohort"),
        ("slot:1-4", "top-4 cohort"),
        ("slot:5-11", "picks 5-11 cohort"),
        ("slot:11-14", "picks 11-14 cohort"),
        ("round:1_late", "late first-round cohort"),
        ("round:2", "second-round cohort"),
        ("status:undrafted", "undrafted cohort"),
        ("debut:1-4", "top-4 cohort"),
        ("debut:1_late", "late first-round cohort"),
        ("debut:2", "second-round cohort"),
        ("debut:undrafted", "undrafted cohort"),
        ("field:rookies tonight", "rookies tonight"),
        ("field:undrafted players", "undrafted players"),
        ("nocolonhere", "his cohort"),
        ("slot:abc-def", "his cohort"),
        ("slot:notarange", "his cohort"),
    ],
)
def test_humanize_cohort_key_never_leaks_raw_key(
    cohort_key: Optional[str], expected: str
) -> None:
    result = humanize_cohort_key(cohort_key)
    assert result == expected
    if cohort_key is not None:
        # The raw machine key (colon-joined prefix:suffix) must never survive
        # translation -- only the field: label (a human phrase to begin with)
        # is passed through verbatim.
        if not cohort_key.startswith("field:"):
            assert ":" not in result


# --------------------------------------------------------------------------- #
# Golden strings -- cohort_rank
# --------------------------------------------------------------------------- #
def test_cohort_rank_rank_one_golden_strings() -> None:
    values = {
        "value": 20.5,
        "rank": 1,
        "of": 8,
        "runner_up": {"who": "2025 Flagg", "value": 18.9},
    }
    pid0 = _pid_for_variant(FactKind.COHORT_RANK, 0)
    pid1 = _pid_for_variant(FactKind.COHORT_RANK, 1)
    fact0 = _fact(kind=FactKind.COHORT_RANK, pid=pid0, cohort="slot:1-4", values=values)
    fact1 = _fact(kind=FactKind.COHORT_RANK, pid=pid1, cohort="slot:1-4", values=values)

    assert render_fact(fact0) == (
        "His 20.5 Game Score is the best mark in the top-4 cohort to date "
        "— ahead of 2025 Flagg (18.9)."
    )
    assert render_fact(fact1) == (
        "Nobody in the top-4 cohort has topped his 20.5 Game Score; "
        "2025 Flagg (18.9) is next closest."
    )
    assert render_chip(fact0) == "#1 of 8 · top-4 cohort"


def test_cohort_rank_not_first_golden_strings() -> None:
    values = {
        "value": 12.0,
        "rank": 3,
        "of": 8,
        "runner_up": {"who": "Leader Peer", "value": 20.0},
    }
    pid0 = _pid_for_variant(FactKind.COHORT_RANK, 0)
    pid1 = _pid_for_variant(FactKind.COHORT_RANK, 1)
    fact0 = _fact(kind=FactKind.COHORT_RANK, pid=pid0, cohort="round:2", values=values)
    fact1 = _fact(kind=FactKind.COHORT_RANK, pid=pid1, cohort="round:2", values=values)

    assert render_fact(fact0) == (
        "His 12 Game Score ranks 3rd of 8 in the second-round cohort, "
        "trailing Leader Peer (20)."
    )
    assert render_fact(fact1) == (
        "Among the second-round cohort, he sits 3rd of 8 at 12 Game Score "
        "— Leader Peer (20) leads the group."
    )
    assert render_chip(fact0) == "3rd of 8 · second-round cohort"


# --------------------------------------------------------------------------- #
# Golden strings -- percentile (incl. gated hedge)
# --------------------------------------------------------------------------- #
def test_percentile_confident_golden_strings() -> None:
    values = {"value": 24.6, "pctl": 96.0, "n_cohort": 12, "gated": False}
    pid0 = _pid_for_variant(FactKind.PERCENTILE, 0)
    pid1 = _pid_for_variant(FactKind.PERCENTILE, 1)
    fact0 = _fact(kind=FactKind.PERCENTILE, pid=pid0, cohort="slot:1-4", values=values)
    fact1 = _fact(kind=FactKind.PERCENTILE, pid=pid1, cohort="slot:1-4", values=values)

    assert render_fact(fact0) == (
        "He's grading at the 96th percentile of the top-4 cohort on Game Score (24.6)."
    )
    assert render_fact(fact1) == (
        "24.6 Game Score puts him in the 96th percentile of the top-4 cohort."
    )
    assert render_chip(fact0) == "96th pctl · top-4 cohort"


def test_ordinal_teens_use_th_not_st_nd_rd() -> None:
    """11/12/13 (and the 111/112/113-style equivalents) are 'th', not the
    naive last-digit 'st'/'nd'/'rd' -- the one ordinal edge case worth
    pinning explicitly."""
    fact = _fact(
        kind=FactKind.PERCENTILE,
        pid=1,
        cohort="slot:1-4",
        values={"value": 3.0, "pctl": 11.0, "n_cohort": 12, "gated": False},
    )
    assert "11th percentile" in render_fact(fact)
    assert render_chip(fact) == "11th pctl · top-4 cohort"


def test_percentile_gated_hedges_in_prose_but_chip_still_renders() -> None:
    values = {"value": 15.0, "pctl": 70.0, "n_cohort": 3, "gated": True}
    fact = _fact(kind=FactKind.PERCENTILE, pid=1, cohort="round:1_late", values=values)

    assert render_fact(fact) == (
        "Early read: his 15 Game Score traces to the 70th percentile of the "
        "late first-round cohort, but the sample is still thin."
    )
    assert render_chip(fact) == "early · 70th pctl · late first-round cohort"


# --------------------------------------------------------------------------- #
# Golden strings -- streak
# --------------------------------------------------------------------------- #
def test_streak_golden_strings() -> None:
    values = {"length": 3, "avg_pctl": 78.4, "avg_value": 19.4, "min_value": 15.0}
    pid0 = _pid_for_variant(FactKind.STREAK, 0)
    pid1 = _pid_for_variant(FactKind.STREAK, 1)
    fact0 = _fact(
        kind=FactKind.STREAK, pid=pid0, cohort="status:undrafted", values=values
    )
    fact1 = _fact(
        kind=FactKind.STREAK, pid=pid1, cohort="status:undrafted", values=values
    )

    assert render_fact(fact0) == (
        "3-game streak at 15+ Game Score, averaging the 78th percentile of "
        "the undrafted cohort."
    )
    assert render_fact(fact1) == (
        "He's strung together 3 straight games at 15+ Game Score — a "
        "78th-percentile average (19.4) over the run within the undrafted cohort."
    )
    assert render_chip(fact0) == "3-game streak · 15+ Game Score"


# --------------------------------------------------------------------------- #
# Golden strings -- self_delta (positive + negative)
# --------------------------------------------------------------------------- #
def test_self_delta_positive_golden_strings() -> None:
    values = {
        "value": 20.0,
        "gp": 4,
        "delta": 5.3,
        "since_year": 2023,
        "prior_value": 14.7,
    }
    pid0 = _pid_for_variant(FactKind.SELF_DELTA, 0)
    pid1 = _pid_for_variant(FactKind.SELF_DELTA, 1)
    fact0 = _fact(kind=FactKind.SELF_DELTA, pid=pid0, values=values)
    fact1 = _fact(kind=FactKind.SELF_DELTA, pid=pid1, values=values)

    assert render_fact(fact0) == (
        "+5.3 Game Score through 4 games versus his 2023 Summer League (14.7)."
    )
    assert render_fact(fact1) == (
        "He's running 5.3 Game Score ahead of his 2023 summer through 4 games."
    )
    assert render_chip(fact0) == "+5.3 Game Score vs 2023"


def test_self_delta_negative_golden_strings() -> None:
    values = {
        "value": 9.4,
        "gp": 4,
        "delta": -6.1,
        "since_year": 2023,
        "prior_value": 15.5,
    }
    pid0 = _pid_for_variant(FactKind.SELF_DELTA, 0)
    pid1 = _pid_for_variant(FactKind.SELF_DELTA, 1)
    fact0 = _fact(kind=FactKind.SELF_DELTA, pid=pid0, values=values)
    fact1 = _fact(kind=FactKind.SELF_DELTA, pid=pid1, values=values)

    assert render_fact(fact0) == (
        "-6.1 Game Score through 4 games versus his 2023 Summer League (15.5)."
    )
    assert render_fact(fact1) == (
        "He's tracking 6.1 Game Score below his 2023 summer through 4 games."
    )
    assert render_chip(fact0) == "-6.1 Game Score vs 2023"


# --------------------------------------------------------------------------- #
# Golden strings -- leads_field
# --------------------------------------------------------------------------- #
def test_leads_field_golden_strings() -> None:
    values = {
        "value": 25.0,
        "rank": 1,
        "of": 9,
        "runner_up": {"who": "Other Rookie", "value": 20.0},
    }
    pid0 = _pid_for_variant(FactKind.LEADS_FIELD, 0)
    pid1 = _pid_for_variant(FactKind.LEADS_FIELD, 1)
    fact0 = _fact(
        kind=FactKind.LEADS_FIELD, pid=pid0, cohort="field:rookies", values=values
    )
    fact1 = _fact(
        kind=FactKind.LEADS_FIELD, pid=pid1, cohort="field:rookies", values=values
    )

    assert render_fact(fact0) == (
        "Leads all rookies tonight at 25 Game Score — Other Rookie (20) is next closest."
    )
    assert render_fact(fact1) == (
        "Nobody among rookies has matched his 25 Game Score tonight; "
        "Other Rookie trails at 20."
    )
    assert render_chip(fact0) == "Leads rookies · 25 Game Score"


# --------------------------------------------------------------------------- #
# Golden strings -- debut_vs_bar (positive + negative; spec's "first SL floor"
# restatement, never "McDonald's game")
# --------------------------------------------------------------------------- #
def test_debut_vs_bar_above_bar_golden_strings() -> None:
    values = {"value": 22.0, "bar": 11.2, "delta": 10.8}
    pid0 = _pid_for_variant(FactKind.DEBUT_VS_BAR, 0)
    pid1 = _pid_for_variant(FactKind.DEBUT_VS_BAR, 1)
    fact0 = _fact(
        kind=FactKind.DEBUT_VS_BAR, pid=pid0, cohort="debut:1-4", values=values
    )
    fact1 = _fact(
        kind=FactKind.DEBUT_VS_BAR, pid=pid1, cohort="debut:1-4", values=values
    )

    assert render_fact(fact0) == (
        "His 22 Game Score debut clears the top-4 cohort bar of 11.2 by 10.8."
    )
    assert render_fact(fact1) == (
        "First Summer League floor: 22 Game Score, 10.8 above the top-4 "
        "cohort's historical debut bar (11.2)."
    )
    assert render_chip(fact0) == "Debut · 22 vs 11.2 Game Score bar"


def test_debut_vs_bar_below_bar_golden_strings() -> None:
    values = {"value": 8.0, "bar": 11.2, "delta": -3.2}
    pid0 = _pid_for_variant(FactKind.DEBUT_VS_BAR, 0)
    pid1 = _pid_for_variant(FactKind.DEBUT_VS_BAR, 1)
    fact0 = _fact(
        kind=FactKind.DEBUT_VS_BAR, pid=pid0, cohort="debut:1-4", values=values
    )
    fact1 = _fact(
        kind=FactKind.DEBUT_VS_BAR, pid=pid1, cohort="debut:1-4", values=values
    )

    assert render_fact(fact0) == (
        "His 8 Game Score debut sits 3.2 below the top-4 cohort bar of 11.2."
    )
    assert render_fact(fact1) == (
        "First Summer League floor: 8 Game Score, 3.2 shy of the top-4 "
        "cohort's historical debut bar (11.2)."
    )


# --------------------------------------------------------------------------- #
# Golden strings -- count_club
# --------------------------------------------------------------------------- #
def test_count_club_golden_strings() -> None:
    values = {"value": 22.0, "threshold": 20.0, "count": 8, "since_year": 2017}
    pid0 = _pid_for_variant(FactKind.COUNT_CLUB, 0)
    pid1 = _pid_for_variant(FactKind.COUNT_CLUB, 1)
    fact0 = _fact(kind=FactKind.COUNT_CLUB, pid=pid0, values=values)
    fact1 = _fact(kind=FactKind.COUNT_CLUB, pid=pid1, values=values)

    assert render_fact(fact0) == (
        "8-player club since 2017 at 20+ Game Score — he's the latest to clear it at 22."
    )
    assert render_fact(fact1) == (
        "Only 8 players have hit 20+ Game Score since 2017; he's now one of them at 22."
    )
    assert render_chip(fact0) == "8-player club · 20+ Game Score"


# --------------------------------------------------------------------------- #
# Golden strings -- first_since (with + without a runner-up)
# --------------------------------------------------------------------------- #
def test_first_since_with_runner_up_golden_strings() -> None:
    values = {
        "value": 5.0,
        "since_year": 2019,
        "runner_up": {"who": "Prior Holder", "value": 4.0},
    }
    pid0 = _pid_for_variant(FactKind.FIRST_SINCE, 0)
    pid1 = _pid_for_variant(FactKind.FIRST_SINCE, 1)
    fact0 = _fact(kind=FactKind.FIRST_SINCE, pid=pid0, metric="ast", values=values)
    fact1 = _fact(kind=FactKind.FIRST_SINCE, pid=pid1, metric="ast", values=values)

    assert render_fact(fact0) == (
        "His 5 assists is the most since 2019, when Prior Holder posted 4."
    )
    assert render_fact(fact1) == (
        "Nobody has reached 5 assists since Prior Holder in 2019."
    )
    assert render_chip(fact0) == "Most since 2019 · 5 assists"


def test_first_since_without_runner_up_golden_strings() -> None:
    values = {"value": 12.0, "since_year": 2017}
    pid0 = _pid_for_variant(FactKind.FIRST_SINCE, 0)
    pid1 = _pid_for_variant(FactKind.FIRST_SINCE, 1)
    fact0 = _fact(kind=FactKind.FIRST_SINCE, pid=pid0, values=values)
    fact1 = _fact(kind=FactKind.FIRST_SINCE, pid=pid1, values=values)

    assert (
        render_fact(fact0) == "His 12 Game Score is the best mark since at least 2017."
    )
    assert render_fact(fact1) == "Nothing since 2017 has matched his 12 Game Score."


# --------------------------------------------------------------------------- #
# render_prose_for_surface + build_facts_payload wiring
# --------------------------------------------------------------------------- #
def test_render_prose_for_surface_selects_then_renders() -> None:
    notable = _fact(
        kind=FactKind.PERCENTILE,
        pid=1,
        values={"value": 24.6, "pctl": 96.0, "n_cohort": 12, "gated": False},
        notability=0.92,
    )
    unremarkable = _fact(
        kind=FactKind.PERCENTILE,
        pid=2,
        values={"value": 14.0, "pctl": 50.0, "n_cohort": 12, "gated": False},
        notability=0.1,
    )
    rendered = render_prose_for_surface(
        [notable, unremarkable], surface=Surface.HERO_TAGLINE
    )
    assert rendered == [render_fact(notable)]


def test_build_facts_payload_marks_hero_selection_only_when_included() -> None:
    fact = _fact(
        kind=FactKind.DEBUT_VS_BAR,
        pid=1,
        cohort="debut:1-4",
        values={"value": 22.0, "bar": 11.2, "delta": 10.8},
    )
    slate_payload = build_facts_payload([fact], prose_surfaces=SLATE_PROSE_SURFACES)
    hero_payload = build_facts_payload([fact], prose_surfaces=SLATE_HERO_PROSE_SURFACES)
    assert "hero_tagline" not in slate_payload[0]["selected_for"]
    assert "tick_note" in slate_payload[0]["selected_for"]
    assert "hero_tagline" in hero_payload[0]["selected_for"]
    assert slate_payload[0]["prose"] == hero_payload[0]["prose"] == render_fact(fact)
    assert slate_payload[0]["chip"] == render_chip(fact)


def test_build_facts_payload_grade_prose_surfaces_are_tick_and_ledger() -> None:
    assert GRADE_PROSE_SURFACES == (Surface.TICK_NOTE, Surface.LEDGER_ECHO)


def test_build_facts_payload_excludes_below_floor_fact_from_prose() -> None:
    weak = _fact(
        kind=FactKind.PERCENTILE,
        pid=1,
        values={"value": 14.0, "pctl": 50.0, "n_cohort": 12, "gated": False},
        notability=0.1,  # below desk_selection.NOTABILITY_FLOOR (0.5)
    )
    payload = build_facts_payload([weak], prose_surfaces=GRADE_PROSE_SURFACES)
    assert payload[0]["selected_for"] == []
    assert payload[0]["prose"] is None
    assert payload[0]["chip"] == render_chip(weak)  # chip still renders regardless


# --------------------------------------------------------------------------- #
# Determinism: double-render byte equality + shuffled-order stability
# --------------------------------------------------------------------------- #
def _matrix_facts() -> list[Fact]:
    """A broad-but-bounded matrix of Facts across every kind/family/edge case.

    Reused by the determinism tests and the banned-copy scan below.
    """
    facts: list[Fact] = []
    cohorts: list[Optional[str]] = [
        "slot:1-4",
        "slot:5-11",
        "slot:11-14",
        "round:1_late",
        "round:2",
        "status:undrafted",
        "debut:1-4",
        "debut:1_late",
        "debut:2",
        "debut:undrafted",
        None,
    ]
    metrics = ["gmsc", "ast", "tov_pct"]

    for pid in range(6):
        for cohort in cohorts:
            for metric in metrics:
                for rank, of in ((1, 8), (3, 8), (8, 8)):
                    facts.append(
                        _fact(
                            kind=FactKind.COHORT_RANK,
                            pid=pid,
                            metric=metric,
                            cohort=cohort,
                            values={
                                "value": 20.5,
                                "rank": rank,
                                "of": of,
                                "runner_up": {"who": "Test Peer", "value": 18.2},
                            },
                        )
                    )
                for pctl, gated in (
                    (96.0, False),
                    (4.0, False),
                    (55.0, False),
                    (70.0, True),
                ):
                    facts.append(
                        _fact(
                            kind=FactKind.PERCENTILE,
                            pid=pid,
                            metric=metric,
                            cohort=cohort,
                            values={
                                "value": 22.1,
                                "pctl": pctl,
                                "n_cohort": 15,
                                "gated": gated,
                            },
                        )
                    )
                for length in (3, 4, 6):
                    facts.append(
                        _fact(
                            kind=FactKind.STREAK,
                            pid=pid,
                            metric=metric,
                            cohort=cohort,
                            values={
                                "length": length,
                                "avg_pctl": 78.0,
                                "avg_value": 19.4,
                                "min_value": 15.0,
                            },
                        )
                    )
                for delta in (5.3, -6.1):
                    facts.append(
                        _fact(
                            kind=FactKind.SELF_DELTA,
                            pid=pid,
                            metric=metric,
                            cohort=cohort,
                            values={
                                "value": 20.0,
                                "gp": 4,
                                "delta": delta,
                                "since_year": 2023,
                                "prior_value": 20.0 - delta,
                            },
                        )
                    )
                facts.append(
                    _fact(
                        kind=FactKind.LEADS_FIELD,
                        pid=pid,
                        metric=metric,
                        cohort=(
                            cohort
                            if cohort and cohort.startswith("field:")
                            else "field:rookies"
                        ),
                        values={
                            "value": 25.0,
                            "rank": 1,
                            "of": 9,
                            "runner_up": {"who": "Other Rookie", "value": 20.0},
                        },
                    )
                )
                for delta in (2.4, -1.8):
                    facts.append(
                        _fact(
                            kind=FactKind.DEBUT_VS_BAR,
                            pid=pid,
                            metric=metric,
                            cohort=cohort,
                            values={"value": 14.0, "bar": 14.0 - delta, "delta": delta},
                        )
                    )
                facts.append(
                    _fact(
                        kind=FactKind.COUNT_CLUB,
                        pid=pid,
                        metric=metric,
                        cohort=cohort,
                        values={
                            "value": 22.0,
                            "threshold": 20.0,
                            "count": 8,
                            "since_year": 2017,
                        },
                    )
                )
                facts.append(
                    _fact(
                        kind=FactKind.FIRST_SINCE,
                        pid=pid,
                        metric=metric,
                        cohort=cohort,
                        values={
                            "value": 12.0,
                            "since_year": 2019,
                            "runner_up": {"who": "Old Holder", "value": 11.0},
                        },
                    )
                )
                facts.append(
                    _fact(
                        kind=FactKind.FIRST_SINCE,
                        pid=pid,
                        metric=metric,
                        cohort=cohort,
                        values={"value": 12.0, "since_year": 2017},
                    )
                )
    return facts


def test_double_render_is_byte_identical() -> None:
    """Rendering the same Fact twice produces byte-identical strings."""
    facts = _matrix_facts()
    first_prose = [render_fact(f) for f in facts]
    second_prose = [render_fact(f) for f in facts]
    assert first_prose == second_prose

    first_chip = [render_chip(f) for f in facts]
    second_chip = [render_chip(f) for f in facts]
    assert first_chip == second_chip


def test_shuffled_matrix_rendering_is_unchanged_per_fact() -> None:
    """Re-rendering the matrix in a different order never changes any one
    Fact's rendered output -- each Fact's string is a pure function of
    itself, never of ordering/position within the batch."""
    facts = _matrix_facts()
    expected = {id(f): (render_fact(f), render_chip(f)) for f in facts}

    # Two distinct, non-identity reorderings.
    reversed_order = list(reversed(facts))
    interleaved = facts[1::2] + facts[0::2]

    for reordered in (reversed_order, interleaved):
        assert len(reordered) == len(facts)
        for f in reordered:
            assert (render_fact(f), render_chip(f)) == expected[id(f)]


def test_stable_variant_index_is_pure_and_deterministic() -> None:
    fact_a = _fact(kind=FactKind.PERCENTILE, pid=7, values={"pctl": 50.0})
    fact_b = _fact(kind=FactKind.PERCENTILE, pid=7, values={"pctl": 99.0})
    # Same (player_id, kind) -> same index regardless of unrelated values.
    assert _stable_variant_index(fact_a, 2) == _stable_variant_index(fact_b, 2)
    assert _stable_variant_index(fact_a, 1) == 0


# --------------------------------------------------------------------------- #
# Banned-copy scan over RENDERED OUTPUT (not source literals) -- ticket #519
# editorial constraint. Every term is checked case-insensitively against
# render_fact/render_chip output for the full matrix above.
# --------------------------------------------------------------------------- #
BANNED_TERMS: tuple[str, ...] = (
    "mcdonald's",
    "mcdonalds",
    "two-way",
    "exhibit-10",
    "exhibit 10",
    "signed a deal",
    "roster spot",
    "roster-spot",
    "audition",
    "career-best",
    "must-win",
    "elimination",
    "showdown",
    "battle",
    "bracket",
    "tournament",
)


def test_rendered_output_matrix_has_no_banned_terms() -> None:
    """Scans actual rendered strings (prose + chips) across a broad Fact
    matrix -- not source code -- for every banned term in the ticket."""
    facts = _matrix_facts()
    assert len(facts) > 500  # sanity: the matrix is actually broad

    offenders: list[tuple[str, str, str]] = []
    for fact in facts:
        for label, rendered in (
            ("prose", render_fact(fact)),
            ("chip", render_chip(fact)),
        ):
            lowered = rendered.lower()
            for term in BANNED_TERMS:
                if term in lowered:
                    offenders.append((term, label, rendered))

    assert offenders == [], f"banned terms found in rendered output: {offenders[:10]}"


def test_rendered_output_payload_has_no_banned_terms() -> None:
    """Same scan, but through the persistence payload (`build_facts_payload`)
    -- covers the JSONB shape actually written to T2/T4, including Fact
    metadata fields (player_label, detector_id, cohort) alongside prose/chip."""
    facts = _matrix_facts()
    payload = build_facts_payload(facts, prose_surfaces=SLATE_HERO_PROSE_SURFACES)

    offenders: list[tuple[str, object]] = []
    for entry in payload:
        blob = str(entry).lower()
        for term in BANNED_TERMS:
            if term in blob:
                offenders.append((term, entry))

    assert offenders == [], f"banned terms found in persisted payload: {offenders[:5]}"
