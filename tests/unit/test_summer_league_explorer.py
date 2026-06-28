"""Unit tests for the Summer League Explorer service (Phase 3: SQL pagination).

Covers query parsing, _player_sort_expr SQL expression mapping, and verifies
that the career-grain SQLAlchemy statement contains ORDER BY, LIMIT, and OFFSET
clauses after the sort/pagination logic is applied.
"""

from __future__ import annotations


from types import SimpleNamespace

from app.services.summer_league.metrics import Box, game_score
from app.services.summer_league_explorer_service import (
    ExplorerQuery,
    PAGE_SIZE,
    _PLAYER_ADVANCED_COLUMNS,
    _PLAYER_STAT_COLUMNS,
    _build_result,
    _compute_player_values,
    _is_single_competition,
    _player_sort_expr,
    parse_query,
    ExplorerColumn,
    ExplorerRow,
    _SORT_KEYS_BY_SUBJECT,
)


# --------------------------------------------------------------------------- #
# Game Score (GmSc)
# --------------------------------------------------------------------------- #


def _player_row(**kw: float) -> SimpleNamespace:
    """A summed-box row stand-in for _compute_player_values (defaults to 0)."""
    fields = {
        "gp": 1,
        "sec": 0.0,
        "pace_sec": 0.0,
        "plus_minus": 0,
        "pts": 0,
        "reb": 0,
        "ast": 0,
        "stl": 0,
        "blk": 0,
        "tov": 0,
        "oreb": 0,
        "dreb": 0,
        "pf": 0,
        "fgm": 0,
        "fga": 0,
        "fg3m": 0,
        "fg3a": 0,
        "ftm": 0,
        "fta": 0,
    }
    fields.update(kw)
    return SimpleNamespace(**fields)


def test_gmsc_is_a_base_column_and_sort_key() -> None:
    """GmSc rides with the always-on base columns (it is additive, not pool-calibrated)."""
    base_keys = {c.key for c in _PLAYER_STAT_COLUMNS}
    adv_keys = {c.key for c in _PLAYER_ADVANCED_COLUMNS}
    assert "gmsc" in base_keys
    assert "gmsc" not in adv_keys
    assert "gmsc" in _SORT_KEYS_BY_SUBJECT["players"]


def test_gmsc_per_game_value_matches_hollinger_average() -> None:
    """per_game GmSc == game_score(summed box) / gp, matching the season materialization."""
    row = _player_row(
        gp=4,
        sec=4 * 30 * 60,
        pts=80,
        fgm=32,
        fga=60,
        ftm=12,
        fta=16,
        oreb=8,
        dreb=20,
        ast=16,
        stl=8,
        blk=4,
        tov=12,
        pf=8,
    )
    expected = round(
        game_score(
            Box(
                pts=80,
                fgm=32,
                fga=60,
                ftm=12,
                fta=16,
                oreb=8,
                dreb=20,
                ast=16,
                stl=8,
                blk=4,
                tov=12,
                pf=8,
            )
        )
        / 4,
        1,
    )
    assert _compute_player_values(row, "per_game")["gmsc"] == expected


def test_gmsc_totals_is_cumulative_per_game_times_gp() -> None:
    """totals GmSc is the cumulative box-score Game Score (per_game * gp)."""
    row = _player_row(
        gp=4,
        sec=4 * 30 * 60,
        pts=80,
        fgm=32,
        fga=60,
        ftm=12,
        fta=16,
        oreb=8,
        dreb=20,
        ast=16,
        stl=8,
        blk=4,
        tov=12,
        pf=8,
    )
    per_game = _compute_player_values(row, "per_game")["gmsc"]
    totals = _compute_player_values(row, "totals")["gmsc"]
    assert totals == round(per_game * 4)


def test_gmsc_sort_expr_per_competition_scales_by_mode() -> None:
    """per_competition / per_game grains expose a raw-label GmSc sort expression.

    Each component is NULL-coalesced so a missing box stat does not poison the
    sort to NULL — keeping it consistent with the coalescing display path.
    """
    from app.services.summer_league_explorer_service import _GMSC_SQL_AGG, _GMSC_SQL_RAW

    assert "0.7 * COALESCE(oreb, 0)" in _GMSC_SQL_RAW
    assert "0.3 * COALESCE(dreb, 0)" in _GMSC_SQL_RAW
    assert "0.4 * COALESCE(pf, 0)" in _GMSC_SQL_RAW
    # Career grain sums each component before applying the same weights.
    assert "COALESCE(SUM(oreb), 0)" in _GMSC_SQL_AGG
    assert "COALESCE(SUM(pf), 0)" in _GMSC_SQL_AGG


def test_gmsc_career_sort_expr_scales_to_displayed_rate() -> None:
    """Career GmSc sorts on the per-mode rate of the summed Game Score."""
    expr = _player_sort_expr("gmsc", "per_game")
    assert "SUM(pts)" in expr
    assert "NULLIF(COUNT(*), 0)" in expr
    # totals mode is the unscaled cumulative aggregate (no per-game division).
    assert "COUNT(*)" not in _player_sort_expr("gmsc", "totals")


# --------------------------------------------------------------------------- #
# parse_query (carried over / extended for Phase 3 completeness)
# --------------------------------------------------------------------------- #


def test_ts_pct_is_advanced_not_base_column() -> None:
    """TS% is categorized as an advanced efficiency stat, not an always-on base column.

    It should be absent from the base players catalog (so it does not show in the
    default career/per_game/multi-competition views) and present in the advanced set
    (shown only for a single adv-eligible per_competition). eFG% stays in base.
    """
    base_keys = {c.key for c in _PLAYER_STAT_COLUMNS}
    adv_keys = {c.key for c in _PLAYER_ADVANCED_COLUMNS}
    assert "ts_pct" not in base_keys
    assert "ts_pct" in adv_keys
    assert "efg_pct" in base_keys  # eFG% deliberately stays a base column
    # TS% remains a valid players sort key (sortable even when the column is hidden).
    assert "ts_pct" in _SORT_KEYS_BY_SUBJECT["players"]


def test_composite_sort_key_coerced_off_non_advanced_query() -> None:
    """Composite sort keys (PER/BPM/…) are first-class at career and per_competition
    grains (#406: advanced metrics visible/sortable at all aggregated grains) and are
    coerced to the default only at ``per_game`` grain, whose SELECT never exposes the
    composite columns (sorting on them would emit ORDER BY on a missing column and 500).
    ts_pct stays valid everywhere since it is box-derived.
    """
    # career → composite key is KEPT (sortable at career grain).
    assert parse_query({"sort": "per", "grain": "career"}).sort == "per"
    # per_competition multi-year → composite key is KEPT (sortable at any per_competition scope).
    assert (
        parse_query(
            {
                "sort": "bpm",
                "grain": "per_competition",
                "year_min": "2023",
                "year_max": "2024",
                "venue": "las_vegas",
            }
        ).sort
        == "bpm"
    )
    # single-competition per_competition → composite key is kept.
    assert (
        parse_query(
            {
                "sort": "per",
                "grain": "per_competition",
                "year_min": "2024",
                "year_max": "2024",
                "venue": "las_vegas",
            }
        ).sort
        == "per"
    )
    # per_game → composite key coerced to the default (no composite columns in the SELECT).
    assert parse_query({"sort": "per", "grain": "per_game"}).sort == "pts"
    # ts_pct is box-derived and valid on any grain.
    assert parse_query({"sort": "ts_pct", "grain": "career"}).sort == "ts_pct"


def test_percent_sort_exprs_force_float_division() -> None:
    """FG%/3P%/FT% sort expressions must force float division so Postgres integer
    truncation doesn't collapse every sub-100% ratio to 0 (4/10 and 9/10 → 0).
    """
    for key in ("fg_pct", "fg3_pct", "ft_pct"):
        expr = _player_sort_expr(key, "per_game")
        assert "1.0" in expr, f"{key} sort expr must force float division: {expr!r}"


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


def test_parse_query_rejects_future_draft_class() -> None:
    """#396: an implausible future draft class is dropped (filter off), not applied."""
    from datetime import date

    next_class = date.today().year + 1
    assert parse_query({"draft_class": str(next_class)}).draft_class == next_class
    assert parse_query({"draft_class": str(next_class + 1)}).draft_class is None
    assert parse_query({"draft_class": "2033"}).draft_class is None
    assert parse_query({"draft_class": "2021"}).draft_class == 2021


def test_parse_query_canonicalizes_country() -> None:
    """#395: a raw ?country=US URL resolves to the canonical dropdown value."""
    assert parse_query({"country": "US"}).country == "United States"
    assert parse_query({"country": "USA"}).country == "United States"
    assert parse_query({"country": "United States"}).country == "United States"
    assert parse_query({"country": ""}).country is None
    assert parse_query({}).country is None


# --------------------------------------------------------------------------- #
# _player_sort_expr mapping
# --------------------------------------------------------------------------- #


def test_player_sort_expr_counting_stats_totals_are_passthrough() -> None:
    """In totals mode, counting stats sort on their raw SUM aggregate."""
    for key in ("pts", "reb", "ast", "stl", "blk", "tov", "fgm", "fga"):
        assert _player_sort_expr(key, "totals") == f"SUM({key})"


def test_player_sort_expr_counting_stats_scale_to_displayed_rate() -> None:
    """Counting stats sort on the displayed per-mode rate, not the raw total.

    This is the #397 fix: a per-game/-36/-100 column must be visually monotonic,
    which requires the ORDER BY to rank on the same rate the cell shows.
    """
    assert (
        _player_sort_expr("pts", "per_game") == "SUM(pts) * 1.0 / NULLIF(COUNT(*), 0)"
    )
    assert "NULLIF(SUM(minutes_seconds), 0)" in _player_sort_expr("pts", "per_36")
    assert "NULLIF(SUM(pace * minutes_seconds), 0)" in _player_sort_expr(
        "pts", "per_100"
    )


def test_player_sort_expr_gp_is_passthrough() -> None:
    """GP is mode-independent and sorts on the SELECT output alias."""
    for mode in ("per_game", "per_36", "per_100", "totals"):
        assert _player_sort_expr("gp", mode) == "gp"


def test_player_sort_expr_percentage_stats_return_sql_expressions() -> None:
    """Percentage sort keys return NULLIF-guarded ratios, mode-independent."""
    for key in ("efg_pct", "fg_pct", "fg3_pct", "ft_pct", "ts_pct"):
        expr = _player_sort_expr(key, "per_game")
        assert expr != key, f"{key!r} should map to an expression, not itself"
        assert "NULLIF" in expr
        # Percentages do not vary by mode.
        assert expr == _player_sort_expr(key, "totals")


def test_player_sort_expr_min_uses_real_column() -> None:
    """'min' sorts by minutes_seconds (per-game in rate modes, total in totals).

    The old expression referenced a non-existent ``sec`` column and raised at
    query time; assert it now references the real ``minutes_seconds`` column.
    """
    assert _player_sort_expr("min", "totals") == "SUM(minutes_seconds)"
    assert (
        _player_sort_expr("min", "per_game")
        == "SUM(minutes_seconds) * 1.0 / NULLIF(COUNT(*), 0)"
    )


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
    """Desc direction puts the highest value first."""
    rows = _make_rows(5)
    cols = [ExplorerColumn("pts", "PTS")]
    q = ExplorerQuery(sort="pts", direction="desc", page=1)
    result = _build_result("teams", cols, rows, q)

    assert result.rows[0].values["pts"] == 4.0
    assert result.rows[-1].values["pts"] == 0.0


def test_build_result_sorts_asc() -> None:
    """Asc direction puts the lowest value first."""
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
    """Advanced sort keys are accepted for a single-competition per_competition query —
    the only scope whose SELECT exposes the composite columns. (On other scopes the
    composite keys coerce to the default; see
    test_composite_sort_key_coerced_off_non_advanced_query.)
    """
    for col in _PLAYER_ADVANCED_COLUMNS:
        q = parse_query(
            {
                "subject": "players",
                "sort": col.key,
                "grain": "per_competition",
                "year_min": "2024",
                "year_max": "2024",
                "venue": "las_vegas",
            }
        )
        assert q.sort == col.key, f"expected sort={col.key!r}, got {q.sort!r}"
