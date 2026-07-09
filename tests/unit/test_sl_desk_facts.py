"""Unit tests for the Summer League Desk commentary fact library (#520).

Pure-logic coverage only -- no DB. One positive and one boundary/none case
per detector (per the ticket's Verification section), plus a contract check
that every emitted Fact carries provenance and serializes cleanly to JSON
(the shape T2/T4's `facts` JSONB columns will persist, #519).
"""

from __future__ import annotations

import json

import pytest

from app.schemas.summer_league_desk import SummerLeagueDeskGrade
from app.services.summer_league.desk_facts import (
    ClubMember,
    CohortPeer,
    Fact,
    FactKind,
    FactSubject,
    GameLine,
    PriorEvent,
    PriorHolder,
    detect_cohort_rank,
    detect_count_club,
    detect_debut_vs_bar,
    detect_first_since,
    detect_leads_field,
    detect_percentile,
    detect_self_delta,
    detect_streak,
)
from app.services.summer_league.desk_grades import GradeRow

SUBJECT = FactSubject(player_id=1, player_label="Test Prospect", competition_id=10)


# --------------------------------------------------------------------------- #
# FactKind contract -- the 8 kinds pinned by behavior spec §11 Stage 1
# --------------------------------------------------------------------------- #
def test_fact_kind_has_exactly_the_eight_spec_kinds() -> None:
    """A hard contract: #518/#519 pattern-match on these exact string values."""
    assert {k.value for k in FactKind} == {
        "cohort_rank",
        "percentile",
        "streak",
        "self_delta",
        "leads_field",
        "debut_vs_bar",
        "count_club",
        "first_since",
    }


# --------------------------------------------------------------------------- #
# Fact.to_dict -- JSON-clean serialization for the T2/T4 `facts` JSONB cols
# --------------------------------------------------------------------------- #
def test_fact_to_dict_round_trips_through_json() -> None:
    fact = detect_debut_vs_bar(
        subject=SUBJECT,
        metric="gmsc",
        debut_cohort_key="debut:1-4",
        subject_value=15.0,
        debut_bar=10.0,
        baseline_version="v1",
    )
    payload = json.loads(json.dumps(fact.to_dict()))
    assert payload["kind"] == "debut_vs_bar"
    assert payload["subject"] == {
        "player_id": 1,
        "player_label": "Test Prospect",
        "competition_id": 10,
    }
    assert payload["provenance"] == {
        "detector_id": "debut_vs_bar",
        "baseline_version": "v1",
        "cohort_key": "debut:1-4",
    }


def test_every_positive_fact_carries_provenance() -> None:
    """DoD: 'Provenance exists on every emitted Fact.'"""
    facts: list[Fact | None] = [
        detect_cohort_rank(
            subject=SUBJECT,
            subject_value=20.0,
            metric="gmsc",
            cohort_key="slot:1-4",
            peers=[CohortPeer("Flagg", 18.9)],
        ),
        detect_percentile(
            subject=SUBJECT,
            grade=GradeRow(
                player_id=1,
                competition_id=10,
                baseline_version="v1",
                cohort_key="slot:1-4",
                subject_value=20.0,
                pctl=96.0,
                grade=SummerLeagueDeskGrade.HOT,
                n_cohort=50,
                gated=False,
            ),
        ),
        detect_debut_vs_bar(
            subject=SUBJECT,
            metric="gmsc",
            debut_cohort_key="debut:1-4",
            subject_value=15.0,
            debut_bar=10.0,
        ),
        detect_first_since(
            subject=SUBJECT,
            metric="gmsc",
            cohort_key="round:2",
            subject_value=20.0,
            current_year=2026,
            since_year=2017,
            most_recent_prior=PriorHolder("X", 19.0, 2019),
        ),
    ]
    for f in facts:
        assert f is not None
        assert f.provenance.detector_id == f.kind.value


# --------------------------------------------------------------------------- #
# cohort_rank
# --------------------------------------------------------------------------- #
def test_detect_cohort_rank_positive_new_number_one() -> None:
    """Subject beats every peer -- rank 1, runner-up is the prior best."""
    fact = detect_cohort_rank(
        subject=SUBJECT,
        subject_value=20.0,
        metric="gmsc",
        cohort_key="slot:1-4",
        peers=[CohortPeer("Flagg", 18.9), CohortPeer("Other", 15.0)],
    )
    assert fact is not None
    assert fact.kind == FactKind.COHORT_RANK
    assert fact.values == {
        "value": 20.0,
        "rank": 1,
        "of": 3,
        "runner_up": {"who": "Flagg", "value": 18.9},
    }
    assert fact.notability == 1.0
    assert fact.provenance.cohort_key == "slot:1-4"


def test_detect_cohort_rank_no_peers_returns_none() -> None:
    fact = detect_cohort_rank(
        subject=SUBJECT,
        subject_value=20.0,
        metric="gmsc",
        cohort_key="slot:1-4",
        peers=[],
    )
    assert fact is None


def test_detect_cohort_rank_lower_is_better_metric() -> None:
    """e.g. turnovers -- fewer is the best rank; runner-up is the peer's min."""
    fact = detect_cohort_rank(
        subject=SUBJECT,
        subject_value=1.0,
        metric="tov_pct",
        cohort_key="slot:1-4",
        peers=[CohortPeer("A", 3.0), CohortPeer("B", 2.0)],
        higher_is_better=False,
    )
    assert fact is not None
    assert fact.values["rank"] == 1
    assert fact.values["runner_up"] == {"who": "B", "value": 2.0}


# --------------------------------------------------------------------------- #
# percentile
# --------------------------------------------------------------------------- #
def _grade(pctl: float, gated: bool) -> GradeRow:
    return GradeRow(
        player_id=1,
        competition_id=10,
        baseline_version="v1",
        cohort_key="slot:1-4",
        subject_value=20.0,
        pctl=pctl,
        grade=SummerLeagueDeskGrade.HOT,
        n_cohort=50,
        gated=gated,
    )


def test_detect_percentile_positive_extreme_ungated() -> None:
    fact = detect_percentile(subject=SUBJECT, grade=_grade(96.0, gated=False))
    assert fact.kind == FactKind.PERCENTILE
    assert fact.values == {
        "value": 20.0,
        "pctl": 96.0,
        "n_cohort": 50,
        "gated": False,
    }
    assert fact.notability == pytest.approx(0.92)


def test_detect_percentile_gated_dampens_notability() -> None:
    """A gated grade shouldn't read as a confident superlative."""
    ungated = detect_percentile(subject=SUBJECT, grade=_grade(96.0, gated=False))
    gated = detect_percentile(subject=SUBJECT, grade=_grade(96.0, gated=True))
    assert gated.notability == pytest.approx(ungated.notability * 0.3)
    assert gated.notability < ungated.notability


# --------------------------------------------------------------------------- #
# streak
# --------------------------------------------------------------------------- #
def test_detect_streak_positive_three_game_run() -> None:
    games = [
        GameLine(value=5.0, cohort_median=10.0, pctl=40.0),  # breaks the run
        GameLine(value=15.0, cohort_median=10.0, pctl=70.0),
        GameLine(value=16.0, cohort_median=10.0, pctl=75.0),
        GameLine(value=18.0, cohort_median=10.0, pctl=80.0),
    ]
    fact = detect_streak(
        subject=SUBJECT, metric="gmsc", cohort_key="slot:1-4", games=games
    )
    assert fact is not None
    assert fact.kind == FactKind.STREAK
    assert fact.values == {
        "length": 3,
        "avg_pctl": 75.0,
        "avg_value": 16.33,
        "min_value": 15.0,
    }
    assert fact.notability == pytest.approx(0.75)


def test_detect_streak_too_short_returns_none() -> None:
    """Only 2 straight qualifying games -- below MIN_STREAK_LENGTH."""
    games = [
        GameLine(value=5.0, cohort_median=10.0, pctl=40.0),  # breaks the run
        GameLine(value=12.0, cohort_median=10.0, pctl=70.0),
        GameLine(value=13.0, cohort_median=10.0, pctl=72.0),
    ]
    fact = detect_streak(
        subject=SUBJECT, metric="gmsc", cohort_key="slot:1-4", games=games
    )
    assert fact is None


def test_detect_streak_run_below_avg_pctl_floor_returns_none() -> None:
    """A qualifying-length run whose average percentile misses the 65 floor."""
    games = [
        GameLine(value=5.0, cohort_median=10.0, pctl=40.0),  # breaks the run
        GameLine(value=11.0, cohort_median=10.0, pctl=55.0),
        GameLine(value=12.0, cohort_median=10.0, pctl=60.0),
        GameLine(value=13.0, cohort_median=10.0, pctl=62.0),
    ]
    fact = detect_streak(
        subject=SUBJECT, metric="gmsc", cohort_key="slot:1-4", games=games
    )
    assert fact is None


def test_detect_streak_missing_game_grain_pctl_breaks_run() -> None:
    """The documented game-grain gap: an unscored game halts the streak."""
    games = [
        GameLine(value=15.0, cohort_median=10.0, pctl=70.0),
        GameLine(value=16.0, cohort_median=10.0, pctl=None),  # no game-grain baseline
        GameLine(value=17.0, cohort_median=10.0, pctl=80.0),
        GameLine(value=18.0, cohort_median=10.0, pctl=85.0),
    ]
    fact = detect_streak(
        subject=SUBJECT, metric="gmsc", cohort_key="slot:1-4", games=games
    )
    # Only the trailing 2 games (17, 18) have a known pctl before hitting the
    # None -- below MIN_STREAK_LENGTH.
    assert fact is None


# --------------------------------------------------------------------------- #
# self_delta
# --------------------------------------------------------------------------- #
def test_detect_self_delta_positive() -> None:
    fact = detect_self_delta(
        subject=SUBJECT,
        metric="gmsc",
        cohort_key="slot:1-4",
        current_value=20.0,
        current_gp=3,
        prior=PriorEvent(year=2025, value=14.0, gp=4),
    )
    assert fact is not None
    assert fact.kind == FactKind.SELF_DELTA
    assert fact.values == {
        "value": 20.0,
        "gp": 3,
        "delta": 6.0,
        "since_year": 2025,
        "prior_value": 14.0,
    }
    assert fact.notability == pytest.approx(0.5)


def test_detect_self_delta_no_prior_year_returns_none() -> None:
    """A debutant has nothing to delta against -- use debut_vs_bar instead."""
    fact = detect_self_delta(
        subject=SUBJECT,
        metric="gmsc",
        cohort_key="slot:1-4",
        current_value=20.0,
        current_gp=3,
        prior=None,
    )
    assert fact is None


def test_detect_self_delta_below_notable_floor_returns_none() -> None:
    fact = detect_self_delta(
        subject=SUBJECT,
        metric="gmsc",
        cohort_key="slot:1-4",
        current_value=15.0,
        current_gp=3,
        prior=PriorEvent(year=2025, value=14.0, gp=4),
    )
    assert fact is None


# --------------------------------------------------------------------------- #
# leads_field
# --------------------------------------------------------------------------- #
def test_detect_leads_field_positive() -> None:
    fact = detect_leads_field(
        subject=SUBJECT,
        subject_value=20.0,
        metric="gmsc",
        field_label="rookies",
        field=[CohortPeer("A", 15.0), CohortPeer("B", 18.0)],
    )
    assert fact is not None
    assert fact.kind == FactKind.LEADS_FIELD
    assert fact.cohort == "field:rookies"
    assert fact.values == {
        "value": 20.0,
        "rank": 1,
        "of": 3,
        "runner_up": {"who": "B", "value": 18.0},
    }
    assert fact.notability == pytest.approx(0.68)


def test_detect_leads_field_does_not_lead_returns_none() -> None:
    fact = detect_leads_field(
        subject=SUBJECT,
        subject_value=20.0,
        metric="gmsc",
        field_label="rookies",
        field=[CohortPeer("A", 25.0), CohortPeer("B", 18.0)],
    )
    assert fact is None


def test_detect_leads_field_lower_is_better_metric() -> None:
    """e.g. fewest turnovers tonight -- subject leads by being lowest."""
    fact = detect_leads_field(
        subject=SUBJECT,
        subject_value=1.0,
        metric="tov_pct",
        field_label="rookies",
        field=[CohortPeer("A", 3.0), CohortPeer("B", 2.0)],
        higher_is_better=False,
    )
    assert fact is not None
    assert fact.values["runner_up"] == {"who": "B", "value": 2.0}


def test_detect_leads_field_empty_field_returns_none() -> None:
    fact = detect_leads_field(
        subject=SUBJECT,
        subject_value=20.0,
        metric="gmsc",
        field_label="rookies",
        field=[],
    )
    assert fact is None


# --------------------------------------------------------------------------- #
# debut_vs_bar
# --------------------------------------------------------------------------- #
def test_detect_debut_vs_bar_positive_beats_bar() -> None:
    fact = detect_debut_vs_bar(
        subject=SUBJECT,
        metric="gmsc",
        debut_cohort_key="debut:1-4",
        subject_value=15.0,
        debut_bar=10.0,
    )
    assert fact.kind == FactKind.DEBUT_VS_BAR
    assert fact.values == {"value": 15.0, "bar": 10.0, "delta": 5.0}
    assert fact.notability == pytest.approx(0.75)


def test_detect_debut_vs_bar_boundary_exactly_at_bar() -> None:
    fact = detect_debut_vs_bar(
        subject=SUBJECT,
        metric="gmsc",
        debut_cohort_key="debut:1-4",
        subject_value=10.0,
        debut_bar=10.0,
    )
    assert fact.values["delta"] == 0.0
    assert fact.notability == pytest.approx(0.5)


# --------------------------------------------------------------------------- #
# count_club
# --------------------------------------------------------------------------- #
def test_detect_count_club_positive() -> None:
    fact = detect_count_club(
        subject=SUBJECT,
        metric="gmsc",
        cohort_key="status:undrafted",
        subject_value=20.0,
        threshold=15.0,
        since_year=2017,
        other_members=[
            ClubMember("A", 16.0, 2019),
            ClubMember("B", 17.0, 2020),
            ClubMember("C", 18.0, 2021),
        ],
    )
    assert fact is not None
    assert fact.kind == FactKind.COUNT_CLUB
    assert fact.values == {
        "value": 20.0,
        "threshold": 15.0,
        "count": 4,
        "since_year": 2017,
    }
    assert fact.notability == pytest.approx(1.0)


def test_detect_count_club_subject_does_not_qualify_returns_none() -> None:
    fact = detect_count_club(
        subject=SUBJECT,
        metric="gmsc",
        cohort_key="status:undrafted",
        subject_value=10.0,
        threshold=15.0,
        since_year=2017,
        other_members=[ClubMember("A", 16.0, 2019)],
    )
    assert fact is None


def test_detect_count_club_no_other_members_returns_none() -> None:
    fact = detect_count_club(
        subject=SUBJECT,
        metric="gmsc",
        cohort_key="status:undrafted",
        subject_value=20.0,
        threshold=15.0,
        since_year=2017,
        other_members=[],
    )
    assert fact is None


# --------------------------------------------------------------------------- #
# first_since
# --------------------------------------------------------------------------- #
def test_detect_first_since_positive_long_gap() -> None:
    fact = detect_first_since(
        subject=SUBJECT,
        metric="gmsc",
        cohort_key="round:2",
        subject_value=20.0,
        current_year=2026,
        since_year=2017,
        most_recent_prior=PriorHolder("X", 19.0, 2019),
    )
    assert fact.kind == FactKind.FIRST_SINCE
    assert fact.values == {
        "value": 20.0,
        "since_year": 2019,
        "runner_up": {"who": "X", "value": 19.0},
    }
    assert fact.notability == pytest.approx(1.0)


def test_detect_first_since_boundary_no_prior_at_floor() -> None:
    """No prior occurrence at all within the window -- notability floor."""
    fact = detect_first_since(
        subject=SUBJECT,
        metric="gmsc",
        cohort_key="round:2",
        subject_value=20.0,
        current_year=2017,
        since_year=2017,
        most_recent_prior=None,
    )
    assert fact.values == {"value": 20.0, "since_year": 2017}
    assert "runner_up" not in fact.values
    assert fact.notability == pytest.approx(0.6)
