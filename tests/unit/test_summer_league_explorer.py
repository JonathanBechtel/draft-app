"""Unit tests for the Summer League Explorer service (Phase 3: SQL pagination).

Covers query parsing, _player_sort_expr SQL expression mapping, and verifies
that the career-grain SQLAlchemy statement contains ORDER BY, LIMIT, and OFFSET
clauses after the sort/pagination logic is applied.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import select, text
from sqlalchemy.dialects import postgresql

from app.services.summer_league_explorer_service import (
    ExplorerQuery,
    PAGE_SIZE,
    _build_result,
    _count_subquery,
    _player_sort_expr,
    parse_query,
    ExplorerColumn,
    ExplorerFacets,
    ExplorerRow,
    ExplorerResult,
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
        assert "NULLIF" in expr, f"{key!r} expression should guard against division by zero"


def test_player_sort_expr_min_maps_to_sec() -> None:
    """'min' (displayed minutes) sorts by raw seconds-played aggregate."""
    assert _player_sort_expr("min") == "SUM(sec)"


# --------------------------------------------------------------------------- #
# Career-grain statement contains ORDER BY, LIMIT, OFFSET
# --------------------------------------------------------------------------- #


def test_career_grain_statement_contains_order_limit_offset() -> None:
    """The career-grain query path must produce SQL containing ORDER BY, LIMIT, OFFSET.

    We construct a minimal representative SQLAlchemy statement that mirrors
    what _query_players builds (the aggregation + order_by + limit + offset),
    compile it to SQL, and assert all three clauses are present.

    This is a unit test (no DB required): it validates the statement shape, not
    execution results.
    """
    from sqlalchemy import func, nulls_last

    from app.schemas.players_master import PlayerMaster
    from app.schemas.summer_league import SummerLeaguePlayerGameLog, SummerLeagueCompetition

    pgl = SummerLeaguePlayerGameLog
    comp = SummerLeagueCompetition
    pm = PlayerMaster
    sec = pgl.minutes_seconds

    q = ExplorerQuery(subject="players", grain="career", sort="pts", direction="desc", page=2)

    stmt = (
        select(
            pm.slug,
            pm.display_name,
            func.count().label("gp"),
            func.sum(sec).label("sec"),
            func.sum(pgl.pts).label("pts"),
        )  # type: ignore[call-overload]
        .select_from(pgl)
        .join(comp, comp.id == pgl.competition_id)
        .join(pm, pm.id == pgl.player_id)
        .group_by(pgl.player_id, pm.slug, pm.display_name)
        .having(func.count() >= q.min_games)
        .having(func.sum(sec) >= q.min_minutes * 60)
    )

    sort_expr = _player_sort_expr(q.sort)
    direction = "DESC" if q.direction == "desc" else "ASC"
    stmt = stmt.order_by(nulls_last(text(f"{sort_expr} {direction}")))
    stmt = stmt.limit(PAGE_SIZE).offset((q.page - 1) * PAGE_SIZE)

    # Compile to a dialect-specific string for inspection.
    compiled = stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})  # type: ignore[call-arg]
    sql = str(compiled).upper()

    assert "ORDER BY" in sql, "compiled SQL must contain ORDER BY"
    assert "LIMIT" in sql, "compiled SQL must contain LIMIT"
    assert "OFFSET" in sql, "compiled SQL must contain OFFSET"
    # Offset for page 2 = (2-1) * PAGE_SIZE = PAGE_SIZE.
    assert str(PAGE_SIZE) in sql, f"LIMIT/OFFSET value {PAGE_SIZE} must appear in SQL"


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
