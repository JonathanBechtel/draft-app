"""Unit tests for T8b (#729): the five hand-derived rollup-taxonomy sites now read
``app.services.stats.registry``'s ``rollup_class`` instead of re-deriving it.

Covers the ticket's Definition of Done directly:

* ``rollup_class_matches`` (the shared live-read helper every site now calls) behaves
  correctly for a registered match, a registered mismatch, and an undeclared key.
* The two historically-misclassified metrics (``ws82``/``vorp82``) assert as
  ``pool_recalibrated`` and, at the Explorer's ``rollup_recombinable`` (site 2), are
  *refused* rather than silently recomputed as if they were recombinable -- the
  structural fix for the bug that classification once caused.
* A recombinable metric (``ts_pct``) is recomputed from summed box components, not
  averaged, at ``rollup_recombinable``.
* An additive-share metric (``ws``) is summed-then-re-shared (WS -> WS/40) at the
  advanced-metrics wiring (``_career``).
* Game Score's linear-recombination shortcut (site 3, ``game_score_line``) matches
  its registry-declared ``recombinable`` class.
* The two flagged-not-resolved conflicts (career TOV% pooling at ``_blend_leader_values``
  and the Class Tracker) are pinned as currently-behaving (minute-weighted, not
  recombined) so a future change to that behavior is a deliberate, visible decision
  rather than an accidental drift.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Optional

import pytest

from app.services.stats.formulas import game_score_line
from app.services.stats.registry import (
    METRICS_BY_KEY,
    RollupClass,
    get_metric,
    require_rollup_class,
    rollup_class_matches,
)
from app.services.summer_league_explorer_service import rollup_recombinable
from app.services.summer_league_metrics_service import (
    PlayerMetricSeason,
    _ADV_BLEND_RATE_COLS,
    _ADV_BLEND_SUM_COLS,
    _career,
)
from app.services.summer_league.desk_read import (
    _ADV_RATE_COMPOSITE_KEYS,
    _build_stat_columns,
)


# --------------------------------------------------------------------------- #
# rollup_class_matches -- the shared live-read helper.
# --------------------------------------------------------------------------- #


def test_rollup_class_matches_true_for_a_registered_key_in_the_expected_class() -> None:
    """A key declared under the expected class matches."""
    assert rollup_class_matches("ts_pct", RollupClass.RECOMBINABLE) is True


def test_rollup_class_matches_false_for_a_registered_key_in_a_different_class() -> None:
    """A key declared under a *different* class does not match."""
    assert rollup_class_matches("ts_pct", RollupClass.POOL_RECALIBRATED) is False
    assert rollup_class_matches("ws82", RollupClass.RECOMBINABLE) is False


def test_rollup_class_matches_false_for_an_undeclared_key() -> None:
    """A key with no registry entry never matches -- not an error, just False."""
    assert rollup_class_matches("fg_pct", RollupClass.RECOMBINABLE) is False
    assert rollup_class_matches("not_a_real_metric", RollupClass.RECOMBINABLE) is False


def test_require_rollup_class_passes_for_declared_keys_in_the_expected_class() -> None:
    """The raise-based guard is silent when every key matches its declaration."""
    require_rollup_class("test site", RollupClass.RECOMBINABLE, "ts_pct", "efg_pct")


def test_require_rollup_class_raises_for_a_reclassified_or_undeclared_key() -> None:
    """The guard raises LookupError naming the site and the offending key.

    This is the ``python -O`` hardening: the adoption sites used bare
    module-level ``assert`` statements, which ``-O`` strips -- dissolving the
    registry cross-check without a trace. ``require_rollup_class`` raises
    unconditionally, so the guard survives any interpreter mode.
    """
    with pytest.raises(LookupError, match=r"test site: 'ws82'"):
        require_rollup_class("test site", RollupClass.RECOMBINABLE, "ts_pct", "ws82")
    with pytest.raises(LookupError, match=r"'not_a_real_metric'"):
        require_rollup_class(
            "test site", RollupClass.RECOMBINABLE, "not_a_real_metric"
        )


def test_rollup_class_matches_accepts_multiple_expected_classes() -> None:
    """Any one of several expected classes is enough to match."""
    assert rollup_class_matches(
        "ws", RollupClass.RECOMBINABLE, RollupClass.ADDITIVE_SHARE
    ) is True


# --------------------------------------------------------------------------- #
# Site 2 -- Explorer's rollup_recombinable now refuses non-recombinable keys.
# --------------------------------------------------------------------------- #


def _box(**kwargs: float) -> SimpleNamespace:
    fields: dict[str, float] = dict(
        pts=0, fgm=0, fga=0, fg3m=0, fg3a=0, ftm=0, fta=0
    )
    fields.update(kwargs)
    return SimpleNamespace(**fields)


def test_ws82_and_vorp82_are_pool_recalibrated() -> None:
    """Pin the known-correct classification T8b adopts (not re-derives)."""
    assert get_metric("ws82").rollup_class is RollupClass.POOL_RECALIBRATED
    assert get_metric("vorp82").rollup_class is RollupClass.POOL_RECALIBRATED


def test_rollup_recombinable_refuses_ws82_and_vorp82() -> None:
    """The historical bug's structural fix: ws82/vorp82 are never recombined here.

    Before the registry-driven gate, nothing stopped a caller from passing a
    pool_recalibrated key like ``ws82`` into ``rollup_recombinable`` -- the
    function would just fall through its elif chain to the "unknown key" catch-all
    and return ``None`` too, but only by accident. Now the refusal is a declared
    contract read from the registry, not a coincidence of an unhandled branch.
    """
    rows = [_box(pts=10, fga=5, fta=2), SimpleNamespace(ws82=8.0, minutes=100.0)]
    assert rollup_recombinable(rows, "ws82") is None
    assert rollup_recombinable(rows, "vorp82") is None


def test_rollup_recombinable_refuses_any_key_the_registry_declares_non_recombinable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal is the registry gate, not the elif chain's unknown-key fallthrough.

    Added by the Phase 2 QA gate (#731). The ws82/vorp82 assertions above hold
    identically with or without T8b's gate — both keys fall off the end of the
    elif chain and return ``None`` either way, so that test cannot distinguish
    the feature from its absence (its own docstring concedes as much). This one
    can: it re-declares ``ts_pct`` — a key the elif chain *does* handle and would
    happily recombine into a number — as ``POOL_RECALIBRATED``, so ``None`` is
    reachable only through the registry-driven refusal. Delete the gate at
    ``app/services/summer_league_explorer_service.py`` and this goes red.
    """
    rows = [_box(pts=10, fga=5, fta=2), _box(pts=8, fga=4, fta=4)]
    assert rollup_recombinable(rows, "ts_pct") is not None  # control

    recalibrated = replace(
        get_metric("ts_pct"), rollup_class=RollupClass.POOL_RECALIBRATED
    )
    monkeypatch.setitem(METRICS_BY_KEY, "ts_pct", recalibrated)
    assert rollup_recombinable(rows, "ts_pct") is None


def test_rollup_recombinable_still_recomputes_ts_pct_from_summed_components() -> None:
    """A genuinely recombinable metric (ts_pct) is unaffected by the new gate."""
    rows = [_box(pts=10, fga=5, fta=2), _box(pts=8, fga=4, fta=4)]
    result = rollup_recombinable(rows, "ts_pct")
    expected = 100.0 * 18 / (2.0 * (9 + 0.44 * 6))
    assert result == pytest.approx(expected, abs=1e-6)


def test_rollup_recombinable_still_falls_through_for_undeclared_keys() -> None:
    """fg_pct/fg3_pct/ft_pct have no registry entry yet -- unaffected by the gate."""
    rows = [_box(fgm=6, fga=12)]
    assert rollup_recombinable(rows, "fg_pct") == pytest.approx(50.0, abs=1e-6)


# --------------------------------------------------------------------------- #
# Site 3 -- game_score_line / gmsc.
# --------------------------------------------------------------------------- #


def test_gmsc_is_declared_recombinable() -> None:
    """The registry class game_score_line's linear-recombination shortcut relies on."""
    assert get_metric("gmsc").rollup_class is RollupClass.RECOMBINABLE


def test_game_score_line_recombination_matches_summed_game_scores() -> None:
    """Recomputing Game Score from summed box totals equals the sum of per-line scores.

    This is the numeric content behind the "linear in the box stats" comment --
    it holds *because* gmsc is recombinable, not despite it.
    """
    line_a = dict(
        pts=20, fgm=8, fga=15, ftm=2, fta=3, oreb=1, dreb=4,
        ast=3, stl=1, blk=0, tov=2, pf=2,
    )
    line_b = dict(
        pts=14, fgm=5, fga=12, ftm=3, fta=4, oreb=2, dreb=5,
        ast=5, stl=2, blk=1, tov=3, pf=1,
    )
    summed = {k: line_a[k] + line_b[k] for k in line_a}
    assert game_score_line(**summed) == pytest.approx(
        game_score_line(**line_a) + game_score_line(**line_b), abs=1e-9
    )


# --------------------------------------------------------------------------- #
# Site 1 -- the advanced-metrics wiring (_career): additive_share sums-then-reshares.
# --------------------------------------------------------------------------- #


def _season(*, minutes: float, ws: Optional[float] = None, vorp: Optional[float] = None) -> PlayerMetricSeason:
    return PlayerMetricSeason(
        year=2025, venue_slug="las_vegas", venue_label="Las Vegas", venue_abbr="LV",
        gp=3, minutes=minutes, pts=0, fga=0, fg3a=0, fta=0, tov=0,
        ts_pct=None, efg_pct=None, gmsc=None, fg3ar=None, ftr=None,
        ast_fgm=None, unast_fgm=None, astd_pct=None,
        usg_pct=None, ast_pct=None, tov_pct=None, orb_pct=None, drb_pct=None,
        trb_pct=None, stl_pct=None, blk_pct=None,
        per=None, ortg=None, drtg=None, net_rtg=None, obpm=None, dbpm=None,
        ws=ws, ows=None, dws=None, ws40=None, ws82=None, bpm=None,
        vorp=vorp, vorp82=None,
    )


def test_ws_and_vorp_are_declared_additive_share() -> None:
    assert get_metric("ws").rollup_class is RollupClass.ADDITIVE_SHARE
    assert get_metric("vorp").rollup_class is RollupClass.ADDITIVE_SHARE


def test_career_sums_ws_then_reshares_into_ws40() -> None:
    """additive_share means: sum the raw values, then re-share into a rate if needed.

    WS is summed directly (1.0 + 2.0); WS/40 is *derived* from that sum, not
    minute-weight-averaged from each pool's own WS/40.
    """
    seasons = [_season(minutes=100.0, ws=1.0, vorp=0.6), _season(minutes=300.0, ws=2.0, vorp=1.4)]
    career = _career(seasons)
    assert career.ws == pytest.approx(3.0)
    assert career.vorp == pytest.approx(2.0)
    # 40 * 3.0 / 400 = 0.3
    assert career.ws40 == pytest.approx(0.3)


# --------------------------------------------------------------------------- #
# Site 4 -- leaders venue/blend (_blend_leader_values): flagged, not resolved.
# --------------------------------------------------------------------------- #


def test_blend_sum_cols_match_registry_additive_share() -> None:
    """ws/vorp within _ADV_BLEND_SUM_COLS are the registry's additive_share entries."""
    assert "ws" in _ADV_BLEND_SUM_COLS
    assert "vorp" in _ADV_BLEND_SUM_COLS
    assert rollup_class_matches("ws", RollupClass.ADDITIVE_SHARE)
    assert rollup_class_matches("vorp", RollupClass.ADDITIVE_SHARE)


def test_blend_rate_cols_tov_pct_is_flagged_conflict_not_resolved() -> None:
    """Pin the known, deliberately-unresolved conflict: tov_pct is registry
    recombinable but _blend_leader_values still minute-weight-averages it
    (T8b / #729 scope discipline -- flag, do not fix).
    """
    assert "tov_pct" in _ADV_BLEND_RATE_COLS
    assert rollup_class_matches("tov_pct", RollupClass.RECOMBINABLE)


# --------------------------------------------------------------------------- #
# Site 5 -- the Class Tracker: same flagged conflict, same pattern.
# --------------------------------------------------------------------------- #


def test_class_tracker_rate_composite_keys_match_registry_pool_recalibrated() -> None:
    """ws82/bpm in the Tracker's advanced set are registry pool_recalibrated."""
    assert "ws82" in _ADV_RATE_COMPOSITE_KEYS
    assert "bpm" in _ADV_RATE_COMPOSITE_KEYS
    assert rollup_class_matches("ws82", RollupClass.POOL_RECALIBRATED)
    assert rollup_class_matches("bpm", RollupClass.POOL_RECALIBRATED)


def test_class_tracker_tov_pct_is_the_same_flagged_conflict() -> None:
    """tov_pct is treated as rate_composite here too, same unresolved conflict."""
    assert "tov_pct" in _ADV_RATE_COMPOSITE_KEYS
    assert rollup_class_matches("tov_pct", RollupClass.RECOMBINABLE)


def _tracker_row(**kwargs: float) -> SimpleNamespace:
    fields: dict[str, object] = {
        "gp": 3, "minutes": 90.0, "pace": None,
        "pts": 0, "reb": 0, "ast": 0, "stl": 0, "blk": 0, "tov": 0,
        "fgm": 0, "fga": 0, "fg3m": 0, "fg3a": 0, "ftm": 0, "fta": 0,
        "per": None, "usg_pct": None, "ast_pct": None, "tov_pct": None,
        "trb_pct": None, "ws82": None, "bpm": None,
    }
    fields.update(kwargs)
    return SimpleNamespace(**fields)


def test_build_stat_columns_advanced_still_recombines_ts_and_weights_tov() -> None:
    """Behavior-preserving check on the Class Tracker's advanced column build.

    ts_pct is recombined from box totals (registry-consistent); tov_pct is
    minute-weight-averaged (the flagged, unresolved conflict, unchanged here).
    """
    rows = [
        _tracker_row(pts=20, fgm=8, fga=15, fta=3, minutes=100.0, tov_pct=8.0),
        _tracker_row(pts=14, fgm=5, fga=12, fta=4, minutes=200.0, tov_pct=12.0),
    ]
    out = _build_stat_columns(rows, "advanced")  # type: ignore[arg-type]
    expected_ts = 100.0 * 34 / (2.0 * (27 + 0.44 * 7))
    assert out["ts_pct"] == pytest.approx(round(expected_ts, 1), abs=1e-6)
    # Minute-weighted mean: (8*100 + 12*200)/300 = 10.666... -> 10.7
    assert out["tov_pct"] == pytest.approx(10.7, abs=1e-6)
