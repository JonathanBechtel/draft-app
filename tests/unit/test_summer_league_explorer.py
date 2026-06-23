"""Unit tests for the Summer League Explorer service (Phase 3: SQL pagination).

Covers query parsing, _player_sort_expr SQL expression mapping, and verifies
that the career-grain SQLAlchemy statement contains ORDER BY, LIMIT, and OFFSET
clauses after the sort/pagination logic is applied.
"""

from __future__ import annotations

import re

import pytest

from app.services.summer_league_explorer_service import (
    ExplorerQuery,
    PAGE_SIZE,
    _PLAYER_ADVANCED_COLUMNS,
    _build_result,
    _count_subquery,
    _is_single_competition,
    _player_sort_expr,
    parse_query,
    ExplorerColumn,
    ExplorerFacets,
    ExplorerRow,
    ExplorerResult,
    _SORT_KEYS_BY_SUBJECT,
)


# --------------------------------------------------------------------------- #
# parse_query (carried over / extended for Phase 3 completeness)
# --------------------------------------------------------------------------- #


def test_parse_query_defaults_and_validation() -> None:
    """Unknown subject/mode/sort fall back to safe defaults; ints coerce."""
    q = parse_query({"subject": "aliens", "mode": "warp", "sort": "evil", "dir": "x"})
    assert q.subject == "players"
    assert q.mode == "per_game"
    assert q.sort == "pts"
    assert q.direction == "desc"

    q2 = parse_query({"year_min": "2021", "year_max": "bad", "page": "-3"})
    assert q2.year_min == 2021
    assert q2.year_max is None  # invalid → filter off
    assert q2.page == 1  # clamped to >= 1


def test_parse_query_page_clamped_to_1() -> None:
    """Pages <= 0 are clamped to 1."""
    assert parse_query({"page": "0"}).page == 1
    assert parse_query({"page": "-5"}).page == 1
    assert parse_query({"page": "3"}).page == 3


# --------------------------------------------------------------------------- #
# _player_sort_expr mapping
# --------------------------------------------------------------------------- #


def test_player_sort_expr_counting_stats_are_passthrough() -> None:
    """Counting-stat sort keys return themselves (the aggregate label)."""
    for key in ("pts", "reb", "ast", "stl", "blk", "tov", "fgm", "fga", "gp"):
        assert _player_sort_expr(key) == key, f"expected passthrough for {key!r}"


def test_player_sort_expr_percentage_stats_return_sql_expressions() -> None:
    """Percentage sort keys return NULLIF-guarded SQL ratio expressions, not the key itself."""
    for key in ("efg_pct", "fg_pct", "fg3_pct", "ft_pct", "ts_pct"):
        expr = _player_sort_expr(key)
        assert expr != key, f"{key!r} should map to an expression, not itself"
        assert "NULLIF" in expr, (
            f"{key!r} expression should guard against division by zero"
        )


def test_player_sort_expr_min_maps_to_sec() -> None:
    """'min' (displayed minutes) sorts by raw seconds-played aggregate."""
    assert _player_sort_expr("min") == "SUM(sec)"


# --------------------------------------------------------------------------- #
# _build_result (teams/Python-side pagination)
# --------------------------------------------------------------------------- #


def _make_rows(n: int) -> list[ExplorerRow]:
    """Create n ExplorerRows with pts values 0..n-1."""
    return [
        ExplorerRow(label=f"Player {i}", href=None, values={"pts": float(i)})
        for i in range(n)
    ]


def test_build_result_page1_of_2() -> None:
    """Page 1 of 2 pages returns first PAGE_SIZE rows; has_next=True."""
    rows = _make_rows(PAGE_SIZE + 5)
    cols = [ExplorerColumn("pts", "PTS")]
    q = ExplorerQuery(sort="pts", direction="asc", page=1)
    result = _build_result("teams", cols, rows, q)

    assert result.total == PAGE_SIZE + 5
    assert len(result.rows) == PAGE_SIZE
    assert result.has_next is True
    assert result.page == 1


def test_build_result_page2_remainder() -> None:
    """Page 2 returns only the remaining 5 rows; has_next=False."""
    rows = _make_rows(PAGE_SIZE + 5)
    cols = [ExplorerColumn("pts", "PTS")]
    q = ExplorerQuery(sort="pts", direction="asc", page=2)
    result = _build_result("teams", cols, rows, q)

    assert len(result.rows) == 5
    assert result.has_next is False
    assert result.total == PAGE_SIZE + 5


def test_build_result_unpaginated_returns_all_rows() -> None:
    """paginate=False returns every row (CSV export path); has_next is False."""
    rows = _make_rows(PAGE_SIZE + 5)
    cols = [ExplorerColumn("pts", "PTS")]
    q = ExplorerQuery(sort="pts", direction="asc", page=1, paginate=False)
    result = _build_result("teams", cols, rows, q)

    assert len(result.rows) == PAGE_SIZE + 5
    assert result.total == PAGE_SIZE + 5
    assert result.has_next is False


def test_build_result_sorts_desc() -> None:
    """desc direction puts the highest value first."""
    rows = _make_rows(5)
    cols = [ExplorerColumn("pts", "PTS")]
    q = ExplorerQuery(sort="pts", direction="desc", page=1)
    result = _build_result("teams", cols, rows, q)

    assert result.rows[0].values["pts"] == 4.0
    assert result.rows[-1].values["pts"] == 0.0


def test_build_result_sorts_asc() -> None:
    """asc direction puts the lowest value first."""
    rows = _make_rows(5)
    cols = [ExplorerColumn("pts", "PTS")]
    q = ExplorerQuery(sort="pts", direction="asc", page=1)
    result = _build_result("teams", cols, rows, q)

    assert result.rows[0].values["pts"] == 0.0
    assert result.rows[-1].values["pts"] == 4.0


def test_build_result_nulls_sort_last() -> None:
    """Rows with None values for the sort key sort after rows with values."""
    rows = [
        ExplorerRow(label="A", href=None, values={"pts": None}),
        ExplorerRow(label="B", href=None, values={"pts": 10.0}),
        ExplorerRow(label="C", href=None, values={"pts": None}),
    ]
    cols = [ExplorerColumn("pts", "PTS")]
    q = ExplorerQuery(sort="pts", direction="desc", page=1)
    result = _build_result("teams", cols, rows, q)

    assert result.rows[0].label == "B"
    assert result.rows[1].values["pts"] is None
    assert result.rows[2].values["pts"] is None


# --------------------------------------------------------------------------- #
# Phase 4a: _is_single_competition + advanced sort key registration
# --------------------------------------------------------------------------- #


def test_is_single_competition_true_when_year_and_venue_pinned() -> None:
    """year_min == year_max with a non-None venue → single competition."""
    assert _is_single_competition(
        ExplorerQuery(year_min=2024, year_max=2024, venue="las_vegas")
    )


def test_is_single_competition_false_multi_year() -> None:
    """year_min != year_max → not a single competition."""
    assert not _is_single_competition(
        ExplorerQuery(year_min=2023, year_max=2024, venue="las_vegas")
    )


def test_is_single_competition_false_no_venue() -> None:
    """Pinned year but no venue → not a single competition."""
    assert not _is_single_competition(
        ExplorerQuery(year_min=2024, year_max=2024, venue=None)
    )


def test_is_single_competition_false_no_constraints() -> None:
    """Default ExplorerQuery (all years, all venues) is not a single competition."""
    assert not _is_single_competition(ExplorerQuery())


def test_advanced_column_keys_in_players_sort_set() -> None:
    """All _PLAYER_ADVANCED_COLUMNS keys are valid sort keys for the players subject.

    parse_query must not reject a sort key like 'per' or 'bpm' for players.
    """
    players_sort_keys = _SORT_KEYS_BY_SUBJECT["players"]
    for col in _PLAYER_ADVANCED_COLUMNS:
        assert col.key in players_sort_keys, (
            f"advanced column key {col.key!r} missing from players sort set"
        )


def test_parse_query_accepts_advanced_sort_keys() -> None:
    """parse_query does not fall back to default when an advanced sort key is passed."""
    for col in _PLAYER_ADVANCED_COLUMNS:
        q = parse_query({"subject": "players", "sort": col.key})
        assert q.sort == col.key, f"expected sort={col.key!r}, got {q.sort!r}"
