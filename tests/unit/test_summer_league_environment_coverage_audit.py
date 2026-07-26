"""Unit tests for the Competition Context Phase 0 coverage audit (#616).

Covers the pure counting/status/coverage logic: final-status classification,
per-source endpoint coverage, DNP exclusion, identity/attribute known/unknown
counts, per-metric certifiability, and all-competitions season summaries. No
database is required.
"""

from __future__ import annotations

import pytest

from scripts.audit_summer_league_environment_coverage import (
    COVERAGE_COMPLETE,
    COVERAGE_PARTIAL,
    COVERAGE_UNAVAILABLE,
    METRIC_REGISTRY,
    SOURCE_BOX,
    SOURCE_SHOT,
    AttributeCoverage,
    CoverageRecord,
    classify_coverage,
    is_appearance,
    roll_up_season,
)


# --------------------------------------------------------------------------- #
# DNP exclusion / appearances
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "minutes_seconds, expected",
    [
        (None, False),  # DNP shell — no minutes recorded
        (0, False),  # played zero seconds — not an appearance
        (1, True),
        (600, True),
    ],
)
def test_is_appearance_excludes_dnp(minutes_seconds, expected) -> None:
    """Only strictly-positive minutes count as an appearance."""
    assert is_appearance(minutes_seconds) is expected


# --------------------------------------------------------------------------- #
# Coverage classification
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "final, covered, expected",
    [
        (0, 0, COVERAGE_UNAVAILABLE),  # no eligible games
        (5, 0, COVERAGE_UNAVAILABLE),  # eligible games but no input
        (5, 5, COVERAGE_COMPLETE),  # full coverage
        (5, 3, COVERAGE_PARTIAL),  # some coverage
        (5, 6, COVERAGE_COMPLETE),  # covered >= final clamps to complete
    ],
)
def test_classify_coverage(final, covered, expected) -> None:
    """Coverage buckets follow complete/partial/unavailable rules."""
    assert classify_coverage(final, covered) == expected


def test_partial_coverage_never_reads_as_zero_or_complete() -> None:
    """A partial box scope must not certify a box metric."""
    rec = CoverageRecord(
        scope_kind="competition",
        scope_key="competition:1",
        year=2025,
        competition_id=1,
        final_games=4,
        box_complete_games=2,
    )
    verdicts = rec.metric_certifiability()
    assert verdicts["pace_per_48"] == COVERAGE_PARTIAL
    assert verdicts["turnover_rate"] == COVERAGE_PARTIAL


# --------------------------------------------------------------------------- #
# Attribute known/unknown counts
# --------------------------------------------------------------------------- #


def test_attribute_coverage_unknown_is_total_minus_known() -> None:
    """Unknown is derived, non-negative, and serializes with a total."""
    cov = AttributeCoverage(known=7, total=10)
    assert cov.unknown == 3
    assert cov.as_dict() == {"known": 7, "unknown": 3, "total": 10}


def test_attribute_coverage_unknown_clamps_at_zero() -> None:
    """Known exceeding total (shouldn't happen) never yields negative unknown."""
    cov = AttributeCoverage(known=5, total=3)
    assert cov.unknown == 0


# --------------------------------------------------------------------------- #
# Per-metric certifiability across sources
# --------------------------------------------------------------------------- #


def test_metric_certifiability_is_source_aware() -> None:
    """Box-complete-but-shot-missing scope certifies box, not shot, metrics."""
    rec = CoverageRecord(
        scope_kind="competition",
        scope_key="competition:9",
        year=2024,
        competition_id=9,
        final_games=6,
        box_complete_games=6,
        shot_covered_games=0,
        pbp_covered_games=0,
        games_with_score=6,
        games_with_known_ot=6,
    )
    verdicts = rec.metric_certifiability()
    # Every box metric is complete.
    box_metrics = [s.key for s in METRIC_REGISTRY if s.source == SOURCE_BOX]
    assert all(verdicts[k] == COVERAGE_COMPLETE for k in box_metrics)
    # Shot metrics are unavailable (no shot input).
    shot_metrics = [s.key for s in METRIC_REGISTRY if s.source == SOURCE_SHOT]
    assert all(verdicts[k] == COVERAGE_UNAVAILABLE for k in shot_metrics)
    # Score/OT metrics are complete.
    assert verdicts["average_score_margin"] == COVERAGE_COMPLETE
    assert verdicts["overtime_share"] == COVERAGE_COMPLETE


def test_metric_certifiability_all_unavailable_when_no_final_games() -> None:
    """A season of only scheduled games certifies nothing."""
    rec = CoverageRecord(
        scope_kind="competition",
        scope_key="competition:3",
        year=2026,
        competition_id=3,
        final_games=0,
    )
    verdicts = rec.metric_certifiability()
    assert set(verdicts.values()) == {COVERAGE_UNAVAILABLE}


# --------------------------------------------------------------------------- #
# Season rollup / summary
# --------------------------------------------------------------------------- #


def _comp(
    comp_id: int,
    venue: str,
    *,
    final: int,
    box: int,
    shot: int = 0,
    player_games: int = 0,
    status_counts: dict[str, int] | None = None,
) -> CoverageRecord:
    """Build a competition record for rollup tests."""
    rec = CoverageRecord(
        scope_kind="competition",
        scope_key=f"competition:{comp_id}",
        year=2025,
        competition_id=comp_id,
        venue_slug=venue,
        final_games=final,
        box_complete_games=box,
        shot_covered_games=shot,
        games_with_score=final,
        games_with_known_ot=final,
        appeared_player_games=player_games,
    )
    if status_counts:
        for status, n in status_counts.items():
            rec.status_counts[status] = n
    return rec


def test_roll_up_season_sums_additive_and_takes_distinct_identities() -> None:
    """Season pools additive counts but takes deduped distinct-player counts."""
    vegas = _comp(1, "las-vegas", final=10, box=10, shot=10, player_games=180)
    cc = _comp(2, "california-classic", final=6, box=4, shot=0, player_games=90)
    # Same-year dedup: 200 distinct canonical people even though the two comps
    # list 150 + 90 appearances (a repeat player at both venues counts once).
    attrs = {
        "draft": AttributeCoverage(known=120, total=200),
        "age": AttributeCoverage(known=150, total=200),
        "position": AttributeCoverage(known=200, total=200),
        "origin": AttributeCoverage(known=60, total=200),
    }
    season = roll_up_season(
        2025,
        [vegas, cc],
        appeared_canonical=200,
        appeared_unresolved=12,
        resolved_appeared=200,
        attributes=attrs,
    )
    assert season.scope_key == "season:2025"
    assert season.included_competitions == 2
    assert season.final_games == 16
    assert season.box_complete_games == 14  # 10 + 4 (partial season box)
    assert season.shot_covered_games == 10
    assert season.appeared_player_games == 270  # additive
    assert season.appeared_canonical == 200  # deduped, not 150 + 90
    assert season.appeared_unresolved == 12
    assert season.attributes["draft"].unknown == 80

    # Season box coverage is partial (14/16) so box metrics do not certify.
    assert season.metric_certifiability()["pace_per_48"] == COVERAGE_PARTIAL


def test_season_status_counts_are_summed() -> None:
    """Scheduled/postponed counts pool across component competitions."""
    a = _comp(1, "a", final=5, box=5, status_counts={"scheduled": 2, "postponed": 1})
    b = _comp(2, "b", final=3, box=3, status_counts={"scheduled": 1, "canceled": 4})
    season = roll_up_season(
        2025,
        [a, b],
        appeared_canonical=0,
        appeared_unresolved=0,
        resolved_appeared=0,
        attributes={},
    )
    assert season.status_counts["scheduled"] == 3
    assert season.status_counts["postponed"] == 1
    assert season.status_counts["canceled"] == 4


def test_flat_dict_exposes_metric_and_attribute_columns() -> None:
    """The machine-readable record carries status, attribute, and metric keys."""
    rec = CoverageRecord(
        scope_kind="competition",
        scope_key="competition:1",
        year=2025,
        competition_id=1,
        venue_slug="las-vegas",
        final_games=4,
        box_complete_games=4,
        games_with_score=4,
        games_with_known_ot=4,
    )
    rec.attributes["draft"] = AttributeCoverage(known=3, total=4)
    flat = rec.to_flat_dict()
    assert flat["scope_key"] == "competition:1"
    assert flat["games_scheduled"] == 0
    assert flat["attr_draft_known"] == 3
    assert flat["attr_draft_unknown"] == 1
    assert flat["metric_pace_per_48"] == COVERAGE_COMPLETE
    assert flat["metric_rim_fg_pct"] == COVERAGE_UNAVAILABLE
