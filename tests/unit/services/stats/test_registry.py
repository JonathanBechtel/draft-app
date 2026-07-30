"""Unit tests for the shared stat-engine metric registry (T7, #724).

Covers the Definition of Done's acceptance criteria directly:

* every registry entry has all required fields populated (no silent ``None`` in
  ``rollup_class``, ``requires``, or ``formula``);
* ``metric_key`` values are unique;
* ``rollup_class`` values come from the closed three-value set;
* the two historically-misclassified metrics (``ws82``, ``vorp82``) assert as
  ``pool_recalibrated``, and ``pace`` (the #732 follow-up's classification) does too;
* ``requires`` for the PBP-derived ``astd_pct`` lists its PBP input, not the box's
  plain ``ast`` field -- this is what T8's capability model (#728) will derive
  availability from;
* the frozen environment ``turnover_rate`` exemption is declared with a distinct
  ``metric_key``/``definition_version`` and a non-empty justification, and is never
  silently absorbed into the engine's own ``tov_pct``;
* ``METRIC_REGISTRY_VERSION`` matches the existing
  ``app.schemas.summer_league_metrics.DEFAULT_METRIC_REGISTRY_VERSION`` constant --
  a regression guard against the exact "second declaration of the same value drifts
  apart" duplication this phase exists to remove, pending Phase 3's re-point of that
  schema constant to import this module's value directly.
"""

from __future__ import annotations

from app.services.stats.registry import (
    METRIC_DEFINITIONS,
    METRIC_REGISTRY_VERSION,
    METRICS_BY_KEY,
    Grain,
    MetricFamily,
    ReferenceKind,
    RollupClass,
    all_metric_keys,
    frozen_exemptions,
    get_metric,
    metrics_by_family,
    metrics_by_rollup_class,
    registry_summary,
    requires_for,
)

_VALID_ROLLUP_CLASSES = {
    RollupClass.RECOMBINABLE,
    RollupClass.ADDITIVE_SHARE,
    RollupClass.POOL_RECALIBRATED,
}


# --------------------------------------------------------------------------- #
# Structural completeness -- every entry is fully populated.
# --------------------------------------------------------------------------- #


def test_registry_is_non_empty() -> None:
    """The registry declares at least the metrics named in the ticket's scope."""
    assert len(METRIC_DEFINITIONS) >= 20


def test_every_entry_has_no_silent_none_in_required_fields() -> None:
    """rollup_class, requires, and formula are never empty/None on any entry."""
    for d in METRIC_DEFINITIONS:
        assert d.rollup_class is not None, d.metric_key
        assert d.requires, f"{d.metric_key} has empty requires"
        assert all(isinstance(r, str) and r for r in d.requires), d.metric_key
        assert d.formula, f"{d.metric_key} has empty formula"
        assert d.definition_version, f"{d.metric_key} has empty definition_version"
        assert d.metric_family is not None, d.metric_key
        assert d.grain_validity, f"{d.metric_key} has empty grain_validity"
        assert d.comparison_semantics, f"{d.metric_key} has empty comparison_semantics"
        assert d.allowed_reference_kinds, f"{d.metric_key} has empty allowed_reference_kinds"
        assert d.minimum_sample_rule, f"{d.metric_key} has empty minimum_sample_rule"
        assert d.coverage_requirement, f"{d.metric_key} has empty coverage_requirement"
        assert d.interpretation_note, f"{d.metric_key} has empty interpretation_note"


def test_grain_validity_values_are_known_grains() -> None:
    """grain_validity only ever names a real Grain member."""
    for d in METRIC_DEFINITIONS:
        assert set(d.grain_validity) <= set(Grain), d.metric_key


def test_allowed_reference_kinds_are_known_reference_kinds() -> None:
    """allowed_reference_kinds only ever names a real ReferenceKind member."""
    for d in METRIC_DEFINITIONS:
        assert set(d.allowed_reference_kinds) <= set(ReferenceKind), d.metric_key


def test_metric_family_values_are_known_families() -> None:
    """Every entry's metric_family is a real MetricFamily member."""
    for d in METRIC_DEFINITIONS:
        assert d.metric_family in set(MetricFamily), d.metric_key


# --------------------------------------------------------------------------- #
# Uniqueness.
# --------------------------------------------------------------------------- #


def test_metric_keys_are_unique() -> None:
    """No metric_key is declared twice -- the whole point of a registry."""
    keys = [d.metric_key for d in METRIC_DEFINITIONS]
    assert len(keys) == len(set(keys))
    assert len(METRICS_BY_KEY) == len(METRIC_DEFINITIONS)


def test_all_metric_keys_matches_definitions() -> None:
    """all_metric_keys() is exactly the set of declared metric_keys, in order."""
    assert all_metric_keys() == tuple(d.metric_key for d in METRIC_DEFINITIONS)


# --------------------------------------------------------------------------- #
# rollup_class -- the closed set, and the two historically-buggy cases.
# --------------------------------------------------------------------------- #


def test_rollup_class_is_always_one_of_the_closed_three_values() -> None:
    """rollup_class never drifts to a fourth, ad-hoc value."""
    for d in METRIC_DEFINITIONS:
        assert d.rollup_class in _VALID_ROLLUP_CLASSES, d.metric_key


def test_ws82_is_pool_recalibrated_not_recombinable() -> None:
    """Known-correct classification: ws82 is a pool-recalibrated rate composite.

    A prior hand-derivation of this taxonomy misclassified ws82 as recombinable,
    which is exactly the bug this registry exists to make structurally impossible
    to repeat -- see the module docstring on RollupClass.
    """
    assert get_metric("ws82").rollup_class is RollupClass.POOL_RECALIBRATED


def test_vorp82_is_pool_recalibrated_not_recombinable() -> None:
    """Known-correct classification: vorp82 is a pool-recalibrated rate composite."""
    assert get_metric("vorp82").rollup_class is RollupClass.POOL_RECALIBRATED


def test_pace_is_pool_recalibrated_per_732() -> None:
    """pace must never be averaged across grains (parked follow-up #732)."""
    assert get_metric("pace").rollup_class is RollupClass.POOL_RECALIBRATED


def test_raw_ws_and_vorp_are_additive_share_not_pool_recalibrated() -> None:
    """Raw WS/VORP are summable across grains, unlike their /82 projections.

    This is the contrast the ws82/vorp82 bug turned on: the raw additive value
    and its pool-recalibrated rate projection must be declared as two different
    rollup classes, not conflated.
    """
    assert get_metric("ws").rollup_class is RollupClass.ADDITIVE_SHARE
    assert get_metric("vorp").rollup_class is RollupClass.ADDITIVE_SHARE


def test_metrics_by_rollup_class_partitions_the_registry() -> None:
    """Every metric appears in exactly one rollup-class bucket."""
    buckets = [metrics_by_rollup_class(rc) for rc in _VALID_ROLLUP_CLASSES]
    total = sum(len(b) for b in buckets)
    assert total == len(METRIC_DEFINITIONS)
    seen_keys: set[str] = set()
    for bucket in buckets:
        for d in bucket:
            assert d.metric_key not in seen_keys
            seen_keys.add(d.metric_key)


# --------------------------------------------------------------------------- #
# requires -- what T8's capability model will derive availability from.
# --------------------------------------------------------------------------- #


def test_astd_pct_requires_lists_its_pbp_input_not_plain_ast() -> None:
    """astd_pct is PBP-derived; its requires must say so, not hide behind box 'ast'.

    T8 (#728) derives computability as ``metric.requires ⊆ source.provides``. A
    source that only provides box totals (no play-by-play) must not appear to
    support astd_pct, which it would if this entry's requires quietly listed the
    box's plain 'ast' field instead of the PBP assist-attribution counts.
    """
    requires = requires_for("astd_pct")
    assert "ast_fgm" in requires
    assert "unast_fgm" in requires
    assert "ast" not in requires


def test_astd_pct_coverage_requirement_names_pbp() -> None:
    """astd_pct's coverage_requirement discloses it needs play-by-play, not just box."""
    assert "pbp" in get_metric("astd_pct").coverage_requirement.lower()


def test_box_only_rate_metrics_do_not_require_pbp_inputs() -> None:
    """A plain box rate (e.g. ts_pct) must not accidentally require PBP fields.

    Guards the other direction of the T8 boundary: a box-only source should be
    able to compute every metric whose requires names only box/team/opponent/
    pool_context tokens.
    """
    pbp_only_tokens = {"ast_fgm", "unast_fgm"}
    for d in METRIC_DEFINITIONS:
        if d.metric_key == "astd_pct":
            continue
        assert not (pbp_only_tokens & set(d.requires)), d.metric_key


# --------------------------------------------------------------------------- #
# The frozen turnover_rate exemption.
# --------------------------------------------------------------------------- #


def test_frozen_turnover_rate_is_declared_as_its_own_exempt_entry() -> None:
    """The environment service's frozen formula gets its own key, not tov_pct's."""
    frozen = get_metric("environment_turnover_rate")
    assert frozen.is_frozen_exemption is True
    assert frozen.exemption_reason
    assert "environment_service" in frozen.exemption_reason


def test_frozen_turnover_rate_has_its_own_definition_version_distinct_from_engine() -> None:
    """The frozen entry's definition_version does not track METRIC_REGISTRY_VERSION.

    It must never move just because the pooled engine's tov_pct/possession
    formula is recalibrated -- that is the entire point of freezing it.
    """
    frozen = get_metric("environment_turnover_rate")
    assert frozen.definition_version != METRIC_REGISTRY_VERSION


def test_non_exempt_entries_have_no_exemption_reason() -> None:
    """Only the declared frozen exemption(s) carry an exemption_reason."""
    for d in METRIC_DEFINITIONS:
        if d.is_frozen_exemption:
            continue
        assert d.exemption_reason is None, d.metric_key


def test_frozen_exemptions_helper_returns_exactly_the_flagged_entries() -> None:
    """frozen_exemptions() is exactly the set of is_frozen_exemption=True entries."""
    exempt = frozen_exemptions()
    assert {d.metric_key for d in exempt} == {
        d.metric_key for d in METRIC_DEFINITIONS if d.is_frozen_exemption
    }
    assert len(exempt) >= 1


def test_engine_tov_pct_and_frozen_environment_rate_are_declared_separately() -> None:
    """The engine's pooled tov_pct is not silently repointed at the frozen formula.

    Both exist as distinct metric_keys with distinct definition_versions -- the
    "flag, do not absorb" decision the ticket calls out explicitly.
    """
    engine = get_metric("tov_pct")
    frozen = get_metric("environment_turnover_rate")
    assert engine.metric_key != frozen.metric_key
    assert engine.is_frozen_exemption is False
    assert engine.definition_version == METRIC_REGISTRY_VERSION


# --------------------------------------------------------------------------- #
# METRIC_REGISTRY_VERSION -- source of the existing constant, not a second one.
# --------------------------------------------------------------------------- #


def test_registry_version_matches_existing_schema_constant() -> None:
    """This registry's version agrees with the pre-existing schema constant.

    Phase 1 (#697) already shipped ``DEFAULT_METRIC_REGISTRY_VERSION`` in
    ``app.schemas.summer_league_metrics``. This ticket (T7) is scoped to new files
    only, so it does not yet re-point that schema constant to import this module's
    value (Phase 3 materialization work, doc #2 §5) -- but it must never mint a
    second, independent version string. This test is the drift guard in the
    meantime: if the two literals are ever bumped out of sync, this fails instead
    of silently diverging, which is exactly the failure mode
    ``registry_version`` exists to prevent one level up (in the materialized
    rows themselves).
    """
    from app.schemas.summer_league_metrics import DEFAULT_METRIC_REGISTRY_VERSION

    assert METRIC_REGISTRY_VERSION == DEFAULT_METRIC_REGISTRY_VERSION


def test_registry_version_is_readable_by_an_external_caller() -> None:
    """The version is a plain importable module-level constant (Phase 3 will read it)."""
    assert isinstance(METRIC_REGISTRY_VERSION, str)
    assert METRIC_REGISTRY_VERSION


# --------------------------------------------------------------------------- #
# Helper functions.
# --------------------------------------------------------------------------- #


def test_get_metric_raises_key_error_for_unknown_key() -> None:
    """get_metric surfaces an unknown key loudly rather than returning None."""
    try:
        get_metric("not_a_real_metric")
    except KeyError:
        pass
    else:  # pragma: no cover - failure path
        raise AssertionError("expected KeyError for an unknown metric key")


def test_metrics_by_family_only_returns_matching_family() -> None:
    """metrics_by_family filters correctly for each declared family."""
    for family in MetricFamily:
        for d in metrics_by_family(family):
            assert d.metric_family is family


def test_registry_summary_counts_match_the_registry() -> None:
    """registry_summary()'s counts add up to the live registry, not a stale copy."""
    summary = registry_summary()
    assert summary.version == METRIC_REGISTRY_VERSION
    assert summary.metric_count == len(METRIC_DEFINITIONS)
    assert sum(summary.rollup_class_counts.values()) == len(METRIC_DEFINITIONS)
