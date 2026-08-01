"""Unit tests for the shared capability model (T8, #728).

Covers the ticket's Definition of Done directly:

* a source declaring box-only inputs yields a metric set with no PBP-derived metrics;
* a source declaring PBP inputs (in addition to box) makes ``astd_pct`` computable;
* a metric with an empty ``requires`` is computable from any source (a synthetic case --
  the live registry's own acceptance criteria guarantee no real entry has an empty
  ``requires``, see ``tests/unit/services/stats/test_registry.py``, so this is exercised
  against a monkeypatched registry rather than a real key);
* the derivation is a pure set operation with no Summer League imports (import contract 3);
* the transitive-requires trap called out in the #728 comment: composite-of-composite
  entries (``ws40.requires == ("ws", "mp")``) resolve to raw inputs, not to the literal
  (and unsatisfiable) metric_key ``"ws"``, and a source providing only those raw inputs
  correctly reports the composite as computable.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.services.stats import capabilities
from app.services.stats.capabilities import (
    computable_metrics,
    is_computable,
    resolve_requires,
)
from app.services.stats.registry import METRICS_BY_KEY, get_metric

# The exact set of raw tokens a box-only source provides -- every StatInputs counting/rate
# field the registry's box-derived metrics name in their requires, per
# app.services.summer_league.capabilities.BOX_PROVIDES (duplicated here as a plain literal so
# this test does not depend on the Summer League adapter -- the capability model itself must
# stand on its own).
_BOX_ONLY_PROVIDES: frozenset[str] = frozenset(
    {
        "fgm",
        "fga",
        "fg3m",
        "fg3a",
        "ftm",
        "fta",
        "oreb",
        "dreb",
        "reb",
        "ast",
        "stl",
        "blk",
        "tov",
        "pf",
        "pts",
        "mp",
    }
)
_ADV_CONTEXT_PROVIDES: frozenset[str] = frozenset({"team_box", "opponent_box", "pool_context"})
_PBP_PROVIDES: frozenset[str] = frozenset({"ast_fgm", "unast_fgm"})


# --------------------------------------------------------------------------- #
# Box-only vs. PBP source -- the ticket's headline acceptance criterion.
# --------------------------------------------------------------------------- #


def test_box_only_source_has_no_pbp_derived_metrics_computable() -> None:
    """A source declaring only box inputs must not report astd_pct as computable.

    astd_pct's requires are ast_fgm/unast_fgm -- PBP tokens deliberately distinct from the
    box's plain 'ast' -- so a box-only provides set must never satisfy them.
    """
    computable = computable_metrics(
        _BOX_ONLY_PROVIDES | _ADV_CONTEXT_PROVIDES
    )
    assert "astd_pct" not in computable
    assert not is_computable("astd_pct", _BOX_ONLY_PROVIDES | _ADV_CONTEXT_PROVIDES)


def test_box_only_source_computes_every_non_pbp_metric_with_satisfied_requires() -> None:
    """Box + team/opponent/pool-context inputs make every other declared metric computable.

    astd_pct is the registry's only PBP-derived entry today; every other metric's
    (transitively resolved) requires are drawn from the box/team/opponent/pool-context
    vocabulary, so a source providing all of it should compute everything except astd_pct.
    """
    provides = _BOX_ONLY_PROVIDES | _ADV_CONTEXT_PROVIDES
    computable = computable_metrics(provides)
    assert computable == frozenset(METRICS_BY_KEY) - {"astd_pct"}


def test_pbp_source_makes_astd_pct_computable() -> None:
    """Adding the PBP tokens to a box-providing source makes astd_pct computable."""
    provides = _BOX_ONLY_PROVIDES | _PBP_PROVIDES
    assert is_computable("astd_pct", provides)
    assert "astd_pct" in computable_metrics(provides)


def test_box_only_source_without_adv_context_cannot_compute_pool_recalibrated_composites() -> (
    None
):
    """A pool that has not earned team/opponent/pool-context still lacks ws/bpm/etc."""
    provides = _BOX_ONLY_PROVIDES  # no team_box/opponent_box/pool_context
    computable = computable_metrics(provides)
    assert "ws" not in computable
    assert "bpm" not in computable
    assert "uper" not in computable
    # Plain box rates stay computable regardless.
    assert "ts_pct" in computable
    assert "tov_pct" in computable


# --------------------------------------------------------------------------- #
# The transitive-requires trap (the #728 comment's critical finding).
# --------------------------------------------------------------------------- #


def test_ws40_resolves_transitively_to_raw_inputs_not_the_literal_ws_key() -> None:
    """ws40.requires == ('ws', 'mp'); resolving must substitute ws's own requires.

    A flat `metric.requires <= provides` set operation would require a source to
    literally provide a field named "ws", which no source ever declares -- this is
    the exact trap the #728 comment warns a naive implementation would fall into.
    """
    resolved = resolve_requires("ws40")
    assert "ws" not in resolved  # the metric_key itself must not leak into the resolved set
    assert "mp" in resolved  # ws40's own direct requires
    # ws's own requires (raw box + team/opponent/pool-context tokens) must be present.
    for token in get_metric("ws").requires:
        assert token in resolved


@pytest.mark.parametrize("key", ["ws40", "net_rating", "ws82", "vorp", "vorp82"])
def test_composite_of_composite_entries_are_computable_from_only_raw_inputs(
    key: str,
) -> None:
    """Every composite-of-composite entry is computable from a box+adv-context source.

    Named in the #728 comment as the entries whose requires list another metric_key:
    ws40 -> ws, net_rating -> ortg/drtg, ws82 -> ws, vorp -> bpm, vorp82 -> bpm. None of
    these raw tokens include a literal metric_key, so a source declaring only
    StatInputs/team/opponent/pool-context tokens must report every one as computable.
    """
    provides = _BOX_ONLY_PROVIDES | _ADV_CONTEXT_PROVIDES
    assert is_computable(key, provides), f"{key} should resolve to only raw inputs"


def test_net_rating_resolves_through_both_ortg_and_drtg() -> None:
    """net_rating.requires == ('ortg', 'drtg') -- both branches must be resolved."""
    resolved = resolve_requires("net_rating")
    assert "ortg" not in resolved
    assert "drtg" not in resolved
    for token in get_metric("ortg").requires:
        assert token in resolved
    for token in get_metric("drtg").requires:
        assert token in resolved


def test_vorp82_resolves_through_bpm() -> None:
    """vorp82.requires == ('bpm', 'mp', 'team_box'); bpm's own requires must appear."""
    resolved = resolve_requires("vorp82")
    assert "bpm" not in resolved
    assert "mp" in resolved
    assert "team_box" in resolved
    for token in get_metric("bpm").requires:
        assert token in resolved


# --------------------------------------------------------------------------- #
# Cycle guard.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _FakeMetric:
    """Minimal stand-in for MetricDefinition -- _resolve only reads .requires."""

    requires: tuple[str, ...]


def test_mutually_referential_requires_raises_instead_of_recursing_forever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cyclical registry entry fails loudly rather than looping forever."""
    fake_registry = {
        "a": _FakeMetric(requires=("b",)),
        "b": _FakeMetric(requires=("a",)),
    }
    monkeypatch.setattr(capabilities, "METRICS_BY_KEY", fake_registry)
    monkeypatch.setattr(capabilities, "get_metric", lambda key: fake_registry[key])

    with pytest.raises(ValueError, match="cycle"):
        resolve_requires("a")


def test_self_referential_requires_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A metric that (invalidly) lists its own key in requires must not infinite-loop."""
    fake_registry = {"self_ref": _FakeMetric(requires=("self_ref",))}
    monkeypatch.setattr(capabilities, "METRICS_BY_KEY", fake_registry)
    monkeypatch.setattr(capabilities, "get_metric", lambda key: fake_registry[key])

    with pytest.raises(ValueError, match="cycle"):
        resolve_requires("self_ref")


# --------------------------------------------------------------------------- #
# Empty requires -- computable from any source (synthetic; the live registry has none).
# --------------------------------------------------------------------------- #


def test_metric_with_empty_requires_is_computable_from_any_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An (synthetic) empty-requires metric is trivially computable, even from nothing.

    The live registry's own acceptance criteria (T7, #724) guarantee no real entry has an
    empty ``requires`` -- see ``test_registry.py::test_every_entry_has_no_silent_none_in_
    required_fields`` -- so this exercises the general subset-of-empty-set property
    directly against a monkeypatched registry rather than a real key.
    """
    fake_registry = {"trivial": _FakeMetric(requires=())}
    monkeypatch.setattr(capabilities, "METRICS_BY_KEY", fake_registry)
    monkeypatch.setattr(capabilities, "get_metric", lambda key: fake_registry[key])

    assert resolve_requires("trivial") == frozenset()
    assert is_computable("trivial", frozenset())
    assert is_computable("trivial", frozenset({"anything"}))
    assert "trivial" in computable_metrics(frozenset())


# --------------------------------------------------------------------------- #
# Unknown key.
# --------------------------------------------------------------------------- #


def test_unknown_metric_key_raises_key_error() -> None:
    """resolve_requires/is_computable surface an unknown key loudly, like get_metric."""
    with pytest.raises(KeyError):
        resolve_requires("not_a_real_metric")


# --------------------------------------------------------------------------- #
# Import contract 3 -- pure set operation, no Summer League coupling.
# --------------------------------------------------------------------------- #


def test_module_has_no_summer_league_imports() -> None:
    """The capability derivation must not import Summer League (import contract 3).

    Parses the module's own AST rather than substring-scanning the file text, because the
    module's docstring legitimately *names* app.services.summer_league.capabilities in
    prose (documenting where the SL-specific mapping lives) without importing it.
    """
    import app.services.stats.capabilities as cap_module

    source = Path(cap_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.append(node.module)

    for module in imported_modules:
        assert not module.startswith("app.services.summer_league"), module
        assert not module.startswith("app.schemas.summer_league"), module
