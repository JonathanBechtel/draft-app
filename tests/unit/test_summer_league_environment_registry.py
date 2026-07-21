"""Unit tests for the Competition Context metric/field registry.

Freeze the v1 contract: exact keys, formulas, units, rounding, coverage source,
zero-denominator behavior, PBP non-gating, and the registry's alignment with the
typed profile columns. No database is required.
"""

from __future__ import annotations

import pytest

from app.schemas.summer_league_environment import SummerLeagueEnvironmentProfile
from app.services import summer_league_environment_registry as reg
from app.services.summer_league_environment_registry import (
    CoverageSource,
    MetricSection,
    MetricUnit,
    ScopeEligibility,
)

# The frozen v1 environment + landscape metric keys (mirrors the coverage audit).
EXPECTED_ENVIRONMENT_KEYS = {
    "points_per_team_game",
    "estimated_possessions",
    "pace_per_48",
    "offensive_rating",
    "three_attempt_share",
    "three_fg_pct",
    "free_throw_rate",
    "offensive_rebound_rate",
    "turnover_rate",
    "assisted_fg_rate",
    "rim_attempt_share",
    "rim_fg_pct",
    "average_score_margin",
    "close_game_share",
    "overtime_share",
}
EXPECTED_LANDSCAPE_KEYS = {
    "team_ortg_iqr",
    "top_decile_minutes_share",
    "top_decile_points_share",
}
EXPECTED_COMPOSITION_KEYS = {
    "distinct_teams",
    "rookie_share",
    "returner_share",
    "drafted_share",
    "undrafted_share",
    "first_round_share",
    "second_round_share",
    "lottery_share",
    "median_age",
}


def test_registry_covers_every_v1_metric() -> None:
    """Registry contains exactly the frozen v1 environment/landscape/composition keys."""
    env = {d.key for d in reg.metrics_in_section(MetricSection.ENVIRONMENT)}
    land = {d.key for d in reg.metrics_in_section(MetricSection.LANDSCAPE)}
    comp = {d.key for d in reg.metrics_in_section(MetricSection.COMPOSITION)}
    assert env == EXPECTED_ENVIRONMENT_KEYS
    assert land == EXPECTED_LANDSCAPE_KEYS
    assert comp == EXPECTED_COMPOSITION_KEYS
    assert set(reg.all_metric_keys()) == (
        EXPECTED_ENVIRONMENT_KEYS | EXPECTED_LANDSCAPE_KEYS | EXPECTED_COMPOSITION_KEYS
    )


def test_metric_keys_are_unique() -> None:
    """No duplicate metric keys in the registry."""
    keys = reg.all_metric_keys()
    assert len(keys) == len(set(keys))


def test_every_definition_carries_required_metadata() -> None:
    """Each entry exposes the full required metadata set.

    Source fields, formula, denominator, unit/scale, rounding, coverage,
    eligibility, definition version, and interpretation must all be present.
    """
    for definition in reg.METRIC_DEFINITIONS:
        assert definition.source_fields, definition.key
        assert definition.formula, definition.key
        assert definition.denominator, definition.key
        assert isinstance(definition.unit, MetricUnit)
        assert definition.scale in (1.0, 100.0)
        assert definition.rounding >= 0
        assert isinstance(definition.coverage_source, CoverageSource)
        assert isinstance(definition.scope_eligibility, ScopeEligibility)
        assert definition.definition_version == reg.REGISTRY_VERSION
        assert definition.interpretation


@pytest.mark.parametrize(
    ("key", "formula", "denominator_starts", "unit", "scale", "rounding", "source"),
    [
        (
            "assisted_fg_rate",
            "sum(ast) / sum(fgm)",
            "made field goals",
            MetricUnit.RATIO,
            100.0,
            1,
            CoverageSource.BOX,
        ),
        (
            "offensive_rating",
            "100 * sum(pts) / sum(possessions)",
            "estimated possessions",
            MetricUnit.RATING,
            1.0,
            1,
            CoverageSource.BOX,
        ),
        (
            "three_attempt_share",
            "sum(fg3a) / sum(fga)",
            "field-goal attempts",
            MetricUnit.RATIO,
            100.0,
            1,
            CoverageSource.BOX,
        ),
        (
            "rim_fg_pct",
            "sum(rim_fgm) / sum(rim_fga)",
            "rim field-goal attempts",
            MetricUnit.RATIO,
            100.0,
            1,
            CoverageSource.SHOT,
        ),
        (
            "close_game_share",
            "count(abs(margin) <= 5) / games_with_score",
            "final games with a known score",
            MetricUnit.RATIO,
            100.0,
            1,
            CoverageSource.SCORE,
        ),
        (
            "overtime_share",
            "count(status_text ILIKE '%OT%') / games_with_known_ot",
            "final games with a known OT state",
            MetricUnit.RATIO,
            100.0,
            1,
            CoverageSource.OT_STATE,
        ),
        (
            "lottery_share",
            "lottery_count / appeared_players",
            "distinct resolved appeared players",
            MetricUnit.RATIO,
            100.0,
            1,
            CoverageSource.IDENTITY,
        ),
        (
            "distinct_teams",
            "count(distinct team_entry_id) pooled over box-complete final games",
            "N/A",
            MetricUnit.COUNT,
            1.0,
            0,
            CoverageSource.BOX,
        ),
    ],
)
def test_exact_metric_definitions(
    key: str,
    formula: str,
    denominator_starts: str,
    unit: MetricUnit,
    scale: float,
    rounding: int,
    source: CoverageSource,
) -> None:
    """Formulas, units, scale, rounding, and coverage source are frozen exactly."""
    d = reg.get_metric(key)
    assert d.formula == formula
    assert d.denominator.startswith(denominator_starts)
    assert d.unit is unit
    assert d.scale == scale
    assert d.rounding == rounding
    assert d.coverage_source is source


def test_assisted_fg_rate_is_box_ast_over_fgm_not_ast_pct() -> None:
    """Assisted-FG rate is the box AST/FGM event rate, never player AST%."""
    d = reg.get_metric("assisted_fg_rate")
    assert d.source_fields == ("ast", "fgm")
    assert d.coverage_source is CoverageSource.BOX
    assert "AST%" in d.interpretation  # explicitly distinguishes it


def test_team_count_is_stored_filterable_and_matches_profile_column() -> None:
    """Team count (#640) reuses the existing distinct_teams profile column.

    Ticket #640: expose the existing distinct_teams projection field through
    the generic fcol/fop/fval threshold contract rather than a new one-off
    param. The metric key must equal the stored column name so
    ``registry_raw_value`` resolves it via ``getattr`` with no extra mapping.
    """
    d = reg.get_metric("distinct_teams")
    assert d.key == "distinct_teams"
    assert d.stored is True
    assert d.filterable is True
    assert d.sortable is True
    assert d.section is MetricSection.COMPOSITION
    assert d.scope_eligibility is ScopeEligibility.BOTH
    columns = set(SummerLeagueEnvironmentProfile.__table__.columns.keys())  # type: ignore[attr-defined]
    assert d.key in columns


def test_team_count_season_scope_meaning_is_documented() -> None:
    """The season-scope team-count meaning is decided and documented (#640).

    A season profile pools ``team_entry_ids`` (a set union) across every
    member competition rather than computing a new franchise-deduplicated
    meaning at request time -- so the same NBA franchise fielding rosters at
    two venues in one summer counts as two team entries. This is asserted in
    the registry's interpretation text so the decision cannot silently drift.
    """
    d = reg.get_metric("distinct_teams")
    assert "season profile" in d.interpretation
    assert "team entries" in d.interpretation


def test_no_v1_metric_is_gated_by_pbp() -> None:
    """PBP is informational only: no displayed metric uses the PBP coverage source."""
    for key in reg.all_metric_keys():
        assert reg.metric_gated_by_pbp(key) is False
        assert reg.get_metric(key).coverage_source is not CoverageSource.PBP


def test_stored_metrics_have_typed_profile_columns() -> None:
    """Every stored metric maps to a typed column on the profile table."""
    columns = set(SummerLeagueEnvironmentProfile.__table__.columns.keys())  # type: ignore[attr-defined]
    for key in reg.stored_metric_keys():
        assert key in columns, key


def test_derived_metric_source_fields_are_profile_columns() -> None:
    """Derived (non-stored) metrics reference stored count columns that exist."""
    columns = set(SummerLeagueEnvironmentProfile.__table__.columns.keys())  # type: ignore[attr-defined]
    for definition in reg.METRIC_DEFINITIONS:
        if definition.stored:
            continue
        for source_field in definition.source_fields:
            assert source_field in columns, (definition.key, source_field)


def test_filterable_metrics_are_typed_or_derived_from_typed() -> None:
    """Filterable values resolve to typed columns, never JSON-only."""
    columns = set(SummerLeagueEnvironmentProfile.__table__.columns.keys())  # type: ignore[attr-defined]
    for key in reg.filterable_metric_keys():
        d = reg.get_metric(key)
        if d.stored:
            assert key in columns
        else:
            assert all(field in columns for field in d.source_fields)


def test_sortable_metric_keys_cover_all_v1_metrics() -> None:
    """Every v1 metric is sortable in the Explorer."""
    assert set(reg.sortable_metric_keys()) == set(reg.all_metric_keys())


def test_safe_ratio_zero_denominator_returns_none() -> None:
    """Zero or missing denominators yield None, never zero and never an error."""
    assert reg.safe_ratio(10, 0) is None
    assert reg.safe_ratio(10, None) is None
    assert reg.safe_ratio(None, 5) is None
    assert reg.safe_ratio(0, 5) == 0.0
    assert reg.safe_ratio(3, 4) == 0.75


def test_format_metric_value() -> None:
    """Display formatting honors unit, scale, and rounding; None -> em dash."""
    # Ratio scaled to percent with one decimal + '%' suffix.
    assert reg.format_metric_value("three_attempt_share", 0.4123) == "41.2%"
    # Rating stored per-100, no suffix.
    assert reg.format_metric_value("offensive_rating", 108.34) == "108.3"
    # Points, one decimal.
    assert reg.format_metric_value("average_score_margin", 12.44) == "12.4"
    # Missing coverage renders as an em dash.
    assert reg.format_metric_value("pace_per_48", None) == "—"
    # Count metric (team count), no decimals, no percent suffix.
    assert reg.format_metric_value("distinct_teams", 8) == "8"


def test_metrics_for_scope_returns_both_eligible() -> None:
    """All v1 metrics are eligible for both scope kinds."""
    season = {d.key for d in reg.metrics_for_scope("season_all_competitions")}
    competition = {d.key for d in reg.metrics_for_scope("competition")}
    assert season == set(reg.all_metric_keys())
    assert competition == set(reg.all_metric_keys())


def test_field_composition_attributes_are_frozen() -> None:
    """The four field-composition attributes are draft/age/position/origin."""
    assert reg.FIELD_COMPOSITION_ATTRIBUTES == ("draft", "age", "position", "origin")


def test_registry_summary_counts() -> None:
    """The registry summary reports consistent section counts."""
    summary = reg.registry_summary()
    assert summary.version == reg.REGISTRY_VERSION
    assert summary.metric_count == len(reg.METRIC_DEFINITIONS)
    assert summary.section_counts["environment"] == len(EXPECTED_ENVIRONMENT_KEYS)
    assert summary.section_counts["landscape"] == len(EXPECTED_LANDSCAPE_KEYS)
    assert summary.section_counts["composition"] == len(EXPECTED_COMPOSITION_KEYS)
