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
from app.schemas.summer_league_metrics import SummerLeaguePlayerSeason
from app.services.summer_league_explorer_service import (
    PLAYER_COLUMN_CATALOG,
    ExplorerColumn,
    MetricFilter,
    _career_metric_having,
    _FILTERABLE_KEYS,
    _per_comp_metric_where,
    _per_game_metric_where,
    _PLAYER_ADVANCED_COLUMNS,
    _PLAYER_STAT_COLUMNS,
    _SORT_KEYS_BY_SUBJECT,
)

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

_CATALOG_BY_KEY: dict[str, ExplorerColumn] = {c.key: c for c in PLAYER_COLUMN_CATALOG}

VALID_BUCKETS = {"recombinable", "additive", "rate_composite"}
VALID_GROUPS = {"box", "shooting", "advanced"}

# All advanced columns present in SummerLeaguePlayerSeason (the full schema set).
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


@pytest.mark.parametrize("key", ["ws", "ows", "dws", "vorp", "gmsc", "ws82", "vorp82"])
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
    """The advanced list is the 7 legacy columns plus the BBRef rate basket (5)."""
    assert len(_PLAYER_ADVANCED_COLUMNS) == 12


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
        "ts_pct", "fg3ar", "ftr", "usg_pct", "ast_pct", "tov_pct",
        "per", "ortg", "drtg", "bpm", "ws", "vorp",
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
_PER_GAME_UNSUPPORTED = {
    "gp", "per", "ortg", "drtg", "bpm", "ws", "vorp",
    "fg3ar", "ftr", "usg_pct", "ast_pct", "tov_pct",
}


@pytest.mark.parametrize("col", sorted(_FILTERABLE_KEYS | {"min"}))
def test_metric_filter_builders_cover_every_column(col: str) -> None:
    """Each builder produces an expression for every filterable column.

    Career (HAVING on aggregates) and per_competition (WHERE on season columns)
    support all filterable columns plus ``min``. Per_game cannot filter advanced
    composites or GP and returns None for those by design.
    """
    f = MetricFilter(col=col, op=">=", value=1.0)

    assert _career_metric_having(f, SummerLeaguePlayerSeason) is not None
    assert _per_comp_metric_where(f, SummerLeaguePlayerSeason) is not None

    per_game_expr = _per_game_metric_where(f, SummerLeaguePlayerGameLog)
    if col in _PER_GAME_UNSUPPORTED:
        assert per_game_expr is None
    else:
        assert per_game_expr is not None


@pytest.mark.parametrize("op", [">=", "<="])
def test_metric_filter_builders_honor_operator(op: str) -> None:
    """Both operators build a valid expression (covers the _op ternary branches)."""
    f = MetricFilter(col="pts", op=op, value=10.0)
    assert _career_metric_having(f, SummerLeaguePlayerSeason) is not None
    assert _per_comp_metric_where(f, SummerLeaguePlayerSeason) is not None
    assert _per_game_metric_where(f, SummerLeaguePlayerGameLog) is not None
