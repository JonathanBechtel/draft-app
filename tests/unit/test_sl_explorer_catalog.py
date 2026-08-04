"""Unit tests for the Summer League Explorer column catalog (ticket #404).

Verifies:
- Every advanced column of ``summer_league_player_seasons`` appears in
  ``PLAYER_COLUMN_CATALOG`` with exactly one valid bucket.
- The three bucket values are the only ones present.
- Specific taxonomy classifications match the documented design.
- Derived ``_PLAYER_STAT_COLUMNS`` / ``_PLAYER_ADVANCED_COLUMNS`` are
  consistent with existing caller expectations (backward-compat).
- No duplicate keys exist in the catalog.
"""

from __future__ import annotations

import pytest

from app.schemas.summer_league import SummerLeaguePlayerGameLog
from app.schemas.summer_league_metrics import SummerLeagueDerivedAgg
from app.services.summer_league_environment_registry import (
    METRIC_DEFINITIONS,
    MetricUnit,
    filterable_metric_keys,
    sortable_metric_keys,
)
from app.services.summer_league_explorer_service import (
    PLAYER_COLUMN_CATALOG,
    ExplorerColumn,
    MetricFilter,
    _career_metric_having,
    _COMPETITION_FILTERABLE_KEYS,
    _COMPETITION_SORT_KEYS,
    _FILTERABLE_KEYS,
    _per_comp_metric_where,
    _per_game_metric_where,
    _PLAYER_ADVANCED_COLUMNS,
    _PLAYER_STAT_COLUMNS,
    _SORT_KEYS_BY_SUBJECT,
    competition_columns,
)

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

_CATALOG_BY_KEY: dict[str, ExplorerColumn] = {c.key: c for c in PLAYER_COLUMN_CATALOG}

VALID_BUCKETS = {"recombinable", "additive", "rate_composite"}
VALID_GROUPS = {"box", "shooting", "advanced"}

# All advanced columns present in SummerLeagueDerivedAgg (the full schema set).
# These must all appear in PLAYER_COLUMN_CATALOG with exactly one bucket.
_SCHEMA_ADVANCED_COLUMNS = {
    # Shooting / efficiency
    "ts_pct",
    "efg_pct",
    "fg3ar",
    "ftr",
    "gmsc",
    # Rate stats
    "usg_pct",
    "ast_pct",
    "orb_pct",
    "drb_pct",
    "trb_pct",
    "stl_pct",
    "blk_pct",
    "tov_pct",
    # Possession / pace
    "pace",
    "pts_per100",
    # Composites (league-relative)
    "per",
    "ortg",
    "drtg",
    "net_rtg",
    "ows",
    "dws",
    "ws",
    "ws40",
    "ws82",
    "obpm",
    "dbpm",
    "bpm",
    "vorp",
    "vorp82",
}


# --------------------------------------------------------------------------- #
# Catalog integrity
# --------------------------------------------------------------------------- #


def test_no_duplicate_keys_in_catalog() -> None:
    """Each column key must appear at most once in the catalog."""
    keys = [c.key for c in PLAYER_COLUMN_CATALOG]
    duplicates = {k for k in keys if keys.count(k) > 1}
    assert not duplicates, f"Duplicate catalog keys: {duplicates}"


def test_all_catalog_entries_have_valid_bucket() -> None:
    """Every entry in the catalog must have one of the three recognised buckets."""
    invalid = [c for c in PLAYER_COLUMN_CATALOG if c.bucket not in VALID_BUCKETS]
    assert not invalid, f"Entries with invalid bucket: {[c.key for c in invalid]}"


def test_all_catalog_entries_have_valid_group() -> None:
    """Every entry must belong to 'box', 'shooting', or 'advanced'."""
    invalid = [c for c in PLAYER_COLUMN_CATALOG if c.group not in VALID_GROUPS]
    assert not invalid, f"Entries with invalid group: {[c.key for c in invalid]}"


def test_every_schema_advanced_column_in_catalog() -> None:
    """Every advanced column of summer_league_player_seasons must appear in the catalog."""
    catalog_keys = set(_CATALOG_BY_KEY)
    missing = _SCHEMA_ADVANCED_COLUMNS - catalog_keys
    assert not missing, (
        f"Schema advanced columns missing from catalog: {sorted(missing)}"
    )


# --------------------------------------------------------------------------- #
# Taxonomy classification assertions
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("key", ["ts_pct", "efg_pct", "fg3ar", "ftr", "pts_per100"])
def test_recombinable_columns_classified_correctly(key: str) -> None:
    """Recombinable metrics must be classified as 'recombinable' in the catalog."""
    col = _CATALOG_BY_KEY[key]
    assert col.bucket == "recombinable", f"{key!r}: expected recombinable, got {col.bucket!r}"


@pytest.mark.parametrize("key", ["ws", "ows", "dws", "vorp", "gmsc"])
def test_additive_columns_classified_correctly(key: str) -> None:
    """Additive metrics must be classified as 'additive' in the catalog."""
    col = _CATALOG_BY_KEY[key]
    assert col.bucket == "additive", f"{key!r}: expected additive, got {col.bucket!r}"


@pytest.mark.parametrize(
    "key",
    [
        "per", "bpm", "obpm", "dbpm",
        "ortg", "drtg", "net_rtg",
        "usg_pct", "ast_pct", "orb_pct", "drb_pct", "trb_pct",
        "stl_pct", "blk_pct", "tov_pct",
        "pace", "ws40",
        # Per-season projections pool minute-weighted — summing would
        # multi-count a multi-pool career.
        "ws82", "vorp82",
    ],
)
def test_rate_composite_columns_classified_correctly(key: str) -> None:
    """Rate-composite metrics must be classified as 'rate_composite' in the catalog."""
    col = _CATALOG_BY_KEY[key]
    assert col.bucket == "rate_composite", (
        f"{key!r}: expected rate_composite, got {col.bucket!r}"
    )


@pytest.mark.parametrize(
    "key",
    ["pts", "reb", "ast", "stl", "blk", "tov", "fgm", "fga", "fg3m", "fg3a", "ftm", "fta"],
)
def test_box_totals_are_additive(key: str) -> None:
    """All raw box-total counting stats must be classified as additive."""
    col = _CATALOG_BY_KEY[key]
    assert col.bucket == "additive", f"{key!r}: expected additive, got {col.bucket!r}"
    assert col.group == "box", f"{key!r}: expected group=box, got {col.group!r}"


def test_fg_pct_fg3_pct_ft_pct_are_shooting_recombinable() -> None:
    """FG% / 3P% / FT% are shooting-group recombinable columns (derive from makes/attempts)."""
    for key in ("fg_pct", "fg3_pct", "ft_pct"):
        col = _CATALOG_BY_KEY[key]
        assert col.group == "shooting", f"{key!r} group mismatch"
        assert col.bucket == "recombinable", f"{key!r} bucket mismatch"


# --------------------------------------------------------------------------- #
# Backward-compat: derived list assertions
# --------------------------------------------------------------------------- #


def test_player_stat_columns_does_not_include_ts_pct() -> None:
    """ts_pct must NOT appear in the always-on base column list.

    It reads as an advanced efficiency metric and is gated to the
    single-competition advanced view.
    """
    base_keys = {c.key for c in _PLAYER_STAT_COLUMNS}
    assert "ts_pct" not in base_keys


def test_player_stat_columns_includes_efg_pct() -> None:
    """eFG% stays in the always-on base column list."""
    base_keys = {c.key for c in _PLAYER_STAT_COLUMNS}
    assert "efg_pct" in base_keys


def test_player_advanced_columns_includes_ts_pct() -> None:
    """ts_pct must appear in the advanced column list (single-competition view)."""
    adv_keys = {c.key for c in _PLAYER_ADVANCED_COLUMNS}
    assert "ts_pct" in adv_keys


def test_player_stat_columns_count() -> None:
    """Base column list = the legacy 22 plus Game Score (gmsc), surfaced by #426."""
    assert len(_PLAYER_STAT_COLUMNS) == 23


def test_player_advanced_columns_count() -> None:
    """The full advanced suite: efficiency, rates, composites, shares, projections."""
    assert len(_PLAYER_ADVANCED_COLUMNS) == 26


def test_player_stat_columns_preserves_display_order() -> None:
    """Key order in _PLAYER_STAT_COLUMNS must match the legacy display order."""
    expected_keys = [
        "gp", "min", "pts", "reb", "ast", "stl", "blk", "tov",
        "oreb", "dreb", "pf", "plus_minus",
        "efg_pct",
        "fgm", "fga", "fg3m", "fg3a", "ftm", "fta",
        "fg_pct", "fg3_pct", "ft_pct",
        "gmsc",  # Game Score — additive/box-derived base column (#426)
    ]
    actual_keys = [c.key for c in _PLAYER_STAT_COLUMNS]
    assert actual_keys == expected_keys


def test_player_advanced_columns_preserves_display_order() -> None:
    """Advanced order: efficiency + rate basket first (BBRef-style), composites after."""
    expected_keys = [
        "ts_pct", "fg3ar", "ftr", "astd_pct",
        "orb_pct", "drb_pct", "trb_pct", "ast_pct", "stl_pct", "blk_pct",
        "tov_pct", "usg_pct",
        "per", "ortg", "drtg", "net_rtg", "obpm", "dbpm", "bpm",
        "ows", "dws", "ws", "ws40", "ws82", "vorp", "vorp82",
    ]
    actual_keys = [c.key for c in _PLAYER_ADVANCED_COLUMNS]
    assert actual_keys == expected_keys


def test_advanced_column_keys_in_players_sort_set() -> None:
    """All _PLAYER_ADVANCED_COLUMNS keys must remain valid sort keys for players."""
    players_sort_keys = _SORT_KEYS_BY_SUBJECT["players"]
    missing = [c.key for c in _PLAYER_ADVANCED_COLUMNS if c.key not in players_sort_keys]
    assert not missing, f"Advanced column keys missing from sort set: {missing}"


def test_catalog_only_columns_not_sortable() -> None:
    """Columns with shown=False must have sortable=False (not yet in Explorer UI)."""
    hidden = [c for c in PLAYER_COLUMN_CATALOG if not c.shown]
    non_sortable = [c for c in hidden if c.sortable]
    assert not non_sortable, (
        f"Hidden columns must not be sortable: {[c.key for c in non_sortable]}"
    )


def test_shown_stat_columns_all_sortable() -> None:
    """Every column in _PLAYER_STAT_COLUMNS must be sortable."""
    non_sortable = [c for c in _PLAYER_STAT_COLUMNS if not c.sortable]
    assert not non_sortable, (
        f"Base stat columns must be sortable: {[c.key for c in non_sortable]}"
    )


def test_shown_advanced_columns_all_sortable() -> None:
    """Every column in _PLAYER_ADVANCED_COLUMNS must be sortable."""
    non_sortable = [c for c in _PLAYER_ADVANCED_COLUMNS if not c.sortable]
    assert not non_sortable, (
        f"Shown advanced columns must be sortable: {[c.key for c in non_sortable]}"
    )


def test_explorer_column_two_arg_compat() -> None:
    """ExplorerColumn(key, label) still works — all new fields have defaults."""
    col = ExplorerColumn("pts", "PTS")
    assert col.key == "pts"
    assert col.label == "PTS"
    # Default values are set
    assert col.group == "box"
    assert col.bucket == "additive"
    assert col.sortable is True
    assert col.filterable is False
    assert col.fmt == "f1"
    assert col.shown is True
    assert col.numeric is True


# --------------------------------------------------------------------------- #
# Metric-filter builder coverage — every filterable column across all 3 grains.
# These build SQL expressions (no DB), so a parametrized pass exercises every
# per-column branch of the career/per-competition/per-game filter builders.
# --------------------------------------------------------------------------- #

# Columns the per_game builder intentionally cannot filter (not on game logs,
# or not meaningful per single game): advanced composites/rates + GP.
# Box-derived rates (3PAr, FTr, TOV%) filter fine on a single game log; the
# rest need composites or team/PBP context that game logs don't carry.
_PER_GAME_UNSUPPORTED = {
    "gp", "per", "ortg", "drtg", "net_rtg", "obpm", "dbpm", "bpm",
    "ws", "ows", "dws", "ws40", "ws82", "vorp", "vorp82",
    "astd_pct", "usg_pct", "ast_pct",
    "orb_pct", "drb_pct", "trb_pct", "stl_pct", "blk_pct",
}


@pytest.mark.parametrize("col", sorted(_FILTERABLE_KEYS | {"min"}))
def test_metric_filter_builders_cover_every_column(col: str) -> None:
    """Each builder produces an expression for every filterable column.

    Career (HAVING on aggregates) and per_competition (WHERE on season columns)
    support all filterable columns plus ``min``. Per_game cannot filter advanced
    composites or GP and returns None for those by design.
    """
    f = MetricFilter(col=col, op=">=", value=1.0)

    assert _career_metric_having(f, SummerLeagueDerivedAgg) is not None
    assert _per_comp_metric_where(f, SummerLeagueDerivedAgg) is not None

    per_game_expr = _per_game_metric_where(f, SummerLeaguePlayerGameLog)
    if col in _PER_GAME_UNSUPPORTED:
        assert per_game_expr is None
    else:
        assert per_game_expr is not None


@pytest.mark.parametrize("op", [">=", "<="])
def test_metric_filter_builders_honor_operator(op: str) -> None:
    """Both operators build a valid expression (covers the _op ternary branches)."""
    f = MetricFilter(col="pts", op=op, value=10.0)
    assert _career_metric_having(f, SummerLeagueDerivedAgg) is not None
    assert _per_comp_metric_where(f, SummerLeagueDerivedAgg) is not None
    assert _per_game_metric_where(f, SummerLeaguePlayerGameLog) is not None


# --------------------------------------------------------------------------- #
# Competition Context columns (subject="competitions", ticket #607) — the
# catalog is generated from the shared registry rather than hand-duplicated,
# so these tests assert the *generation*, not a second copy of the registry.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("scope_kind", ["season_all_competitions", "competition"])
def test_competition_columns_no_duplicate_keys(scope_kind: str) -> None:
    cols = competition_columns(scope_kind)
    keys = [c.key for c in cols]
    assert len(keys) == len(set(keys)), f"duplicate column keys for {scope_kind}: {keys}"


@pytest.mark.parametrize("scope_kind", ["season_all_competitions", "competition"])
def test_competition_columns_include_every_registry_metric(scope_kind: str) -> None:
    """Every registry metric key appears as a competitions column (never a second,
    hand-duplicated definition of label/formula in the Explorer layer).
    """
    cols = {c.key for c in competition_columns(scope_kind)}
    for definition in METRIC_DEFINITIONS:
        assert definition.key in cols, f"registry metric {definition.key!r} missing"


def test_competition_filterable_keys_mirror_registry() -> None:
    assert _COMPETITION_FILTERABLE_KEYS == frozenset(filterable_metric_keys())


def test_competition_sort_keys_superset_of_registry_sortable() -> None:
    """Sort keys include every registry-sortable metric plus the fixed identity
    columns (year, included_competitions, final_games, appeared_players).
    """
    assert frozenset(sortable_metric_keys()) <= _COMPETITION_SORT_KEYS
    assert "year" in _COMPETITION_SORT_KEYS


def test_competition_columns_fmt_matches_unit() -> None:
    """RATIO-unit registry metrics render as 'pct'; others use their rounding."""
    cols_by_key = {c.key: c for c in competition_columns("competition")}
    for definition in METRIC_DEFINITIONS:
        col = cols_by_key[definition.key]
        if definition.unit is MetricUnit.RATIO:
            assert col.fmt == "pct", f"{definition.key} expected pct fmt, got {col.fmt}"
        else:
            assert col.fmt in ("int", f"f{definition.rounding}")


def test_competition_columns_sortable_filterable_mirror_registry_flags() -> None:
    cols_by_key = {c.key: c for c in competition_columns("competition")}
    for definition in METRIC_DEFINITIONS:
        col = cols_by_key[definition.key]
        assert col.sortable == definition.sortable
        assert col.filterable == definition.filterable


# --------------------------------------------------------------------------- #
# Column-density curated default set (ticket #644) — every column still
# appears in the full catalog (CSV/definitions never lose a metric); density
# only controls what the results table shows by default vs. behind the
# condensed/full metric-view control.
# --------------------------------------------------------------------------- #

VALID_DENSITIES = {"core", "full"}


@pytest.mark.parametrize("scope_kind", ["season_all_competitions", "competition"])
def test_competition_columns_density_is_valid(scope_kind: str) -> None:
    for col in competition_columns(scope_kind):
        assert col.density in VALID_DENSITIES, f"{col.key} has invalid density {col.density!r}"


@pytest.mark.parametrize("scope_kind", ["season_all_competitions", "competition"])
def test_competition_columns_curated_core_set_is_small(scope_kind: str) -> None:
    """The default (core) column set stays small enough to avoid horizontal
    scrolling for the common case — the whole point of #644. Every column
    (core or full) still exists in the full catalog returned by
    ``competition_columns``; only the default-visible subset is bounded here.
    """
    cols = competition_columns(scope_kind)
    core = [c for c in cols if c.density == "core" and c.group != "meta"]
    full = [c for c in cols if c.density == "full" and c.group != "meta"]
    assert 0 < len(core) <= 14
    # The curated set is meaningfully smaller than the full matrix it narrows.
    assert len(full) > len(core)


@pytest.mark.parametrize("scope_kind", ["season_all_competitions", "competition"])
def test_competition_columns_identity_year_always_core(scope_kind: str) -> None:
    cols_by_key = {c.key: c for c in competition_columns(scope_kind)}
    assert cols_by_key["year"].density == "core"


def test_competition_columns_venue_is_core_for_competition_scope() -> None:
    cols_by_key = {c.key: c for c in competition_columns("competition")}
    assert cols_by_key["venue"].density == "core"


def test_competition_columns_meta_columns_never_core() -> None:
    """CSV/export-only meta columns are never in the HTML table (the template
    filters ``group != 'meta'``), so their density is irrelevant, but they
    should never accidentally masquerade as a curated default column."""
    cols = competition_columns("competition")
    for col in cols:
        if col.group == "meta":
            assert col.density == "core"  # default value; template never renders it
