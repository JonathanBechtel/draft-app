"""Unit tests for the Summer League Explorer service (Phase 3: SQL pagination).

Covers query parsing, _player_sort_expr SQL expression mapping, and verifies
that the career-grain SQLAlchemy statement contains ORDER BY, LIMIT, and OFFSET
clauses after the sort/pagination logic is applied.
"""

from __future__ import annotations


from types import SimpleNamespace

from datetime import datetime, timedelta

from app.schemas.summer_league_environment import SummerLeagueEnvironmentProfile
from app.services.summer_league.metrics import Box, game_score
from app.services.summer_league_environment_registry import get_metric
from app.services.summer_league_explorer_service import (
    ExplorerQuery,
    PAGE_SIZE,
    STALE_AFTER_HOURS,
    _COMPETITION_FILTERABLE_KEYS,
    _PLAYER_ADVANCED_COLUMNS,
    _PLAYER_STAT_COLUMNS,
    _build_profile_view,
    _build_result,
    _build_trend,
    _compute_player_values,
    _is_single_competition,
    _passes_coverage_filter,
    _passes_metric_filter,
    _player_sort_expr,
    _sort_competition_views,
    _view_to_detail,
    _view_to_row,
    parse_query,
    parse_metric_filters,
    competition_columns,
    ExplorerColumn,
    ExplorerRow,
    MetricFilter,
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


# --------------------------------------------------------------------------- #
# Competition Context (subject="competitions", ticket #607)
# --------------------------------------------------------------------------- #


def _profile(**overrides: object) -> SummerLeagueEnvironmentProfile:
    """A minimal, unpersisted profile row for pure-Python read-adapter tests."""
    defaults: dict[str, object] = dict(
        id=1,
        scope_key="season:2024",
        scope_kind="season_all_competitions",
        year=2024,
        competition_id=None,
        venue_slug=None,
        display_name="2024 Summer League (All Competitions)",
        version=1,
        is_current=True,
        registry_version="2026.07.1",
        calculation_version="2026.07.2",
        included_competitions=2,
        final_games=20,
        scheduled_games=0,
        box_complete_games=20,
        shot_covered_games=20,
        pbp_covered_games=0,
        games_with_score=20,
        games_with_known_ot=20,
        appeared_players=100,
        appeared_unresolved=10,
        rookie_count=40,
        returner_count=60,
        drafted_count=50,
        undrafted_count=50,
        first_round_count=20,
        second_round_count=30,
        lottery_count=10,
        teams_represented=6,
        median_age=21.5,
        # A representative box-derived metric.
        pace_per_48=95.5,
        offensive_rating=1.05,  # stored as a raw ratio-like value for this test
        three_attempt_share=0.35,
        calculated_at=datetime.utcnow(),
    )
    defaults.update(overrides)
    return SummerLeagueEnvironmentProfile(**defaults)  # type: ignore[arg-type]


def test_parse_query_competitions_defaults() -> None:
    """subject=competitions defaults to season scope, coverage=all, sort=year, min_gp=0."""
    q = parse_query({"subject": "competitions"})
    assert q.subject == "competitions"
    assert q.profile_scope == "season"
    assert q.coverage == "all"
    assert q.sort == "year"
    assert q.min_games == 0
    assert q.competition_id is None
    assert q.detail_year is None


def test_parse_query_competitions_season_clears_venue_and_competition_id() -> None:
    """Season scope canonicalization clears venue/competition_id (contract §6)."""
    q = parse_query(
        {
            "subject": "competitions",
            "profile_scope": "season",
            "venue": "las_vegas",
            "competition_id": "7",
        }
    )
    assert q.profile_scope == "season"
    assert q.venue is None
    assert q.competition_id is None


def test_parse_query_competitions_competition_id_clears_detail_year() -> None:
    """competition_id is authoritative for detail; a stale detail_year is dropped."""
    q = parse_query(
        {
            "subject": "competitions",
            "profile_scope": "competition",
            "competition_id": "42",
            "detail_year": "2019",
        }
    )
    assert q.competition_id == 42
    assert q.detail_year is None


def test_parse_query_competitions_invalid_profile_scope_and_coverage_degrade() -> None:
    """Garbage profile_scope/coverage/trend_metric degrade to defaults, never raise."""
    q = parse_query(
        {
            "subject": "competitions",
            "profile_scope": "bogus",
            "coverage": "bogus",
            "trend_metric": "not_a_metric",
        }
    )
    assert q.profile_scope == "season"
    assert q.coverage == "all"
    assert q.trend_metric is None


def test_parse_query_competitions_valid_trend_metric_accepted() -> None:
    """A registered metric key is accepted verbatim as trend_metric."""
    q = parse_query({"subject": "competitions", "trend_metric": "pace_per_48"})
    assert q.trend_metric == "pace_per_48"


def test_parse_query_competitions_min_gp_explicit_zero_still_zero() -> None:
    """An explicit min_gp=0 round-trips (distinguishing 'unset' from 'zero')."""
    q = parse_query({"subject": "competitions", "min_gp": "5"})
    assert q.min_games == 5


def test_parse_metric_filters_uses_registry_keys_for_competitions() -> None:
    """A player-only key (e.g. 'pts') is not a valid competitions threshold column,
    but a registry metric key (e.g. 'pace_per_48') is — the same fcol/fop/fval
    contract, different valid-key vocabulary per subject (contract §6).
    """
    params = {"fcol0": "pace_per_48", "fop0": "gte", "fval0": "90"}
    filters = parse_metric_filters(params, _COMPETITION_FILTERABLE_KEYS)
    assert filters == [MetricFilter(col="pace_per_48", op=">=", value=90.0)]

    filters_player_key = parse_metric_filters(params.copy() | {"fcol0": "pts"}, _COMPETITION_FILTERABLE_KEYS)
    assert filters_player_key == []


# --------------------------------------------------------------------------- #
# Invalid-parameter validation state (ticket #636)
# --------------------------------------------------------------------------- #


def test_parse_metric_filters_records_errors_for_attempted_bad_rows() -> None:
    """Unknown key, bad operator, non-numeric value, and incomplete predicates
    each leave a visible note; a fully blank row (never attempted) does not."""
    errors: list[str] = []
    filters = parse_metric_filters(
        {
            "fcol0": "not_a_metric",
            "fop0": "gte",
            "fval0": "10",
            "fcol1": "pace_per_48",
            "fop1": "bogus_op",
            "fval1": "10",
            "fcol2": "pace_per_48",
            "fop2": "gte",
            "fval2": "abc",
        },
        _COMPETITION_FILTERABLE_KEYS,
        errors=errors,
    )
    assert filters == []
    assert len(errors) == 3

    errors.clear()
    filters = parse_metric_filters(
        {"fcol0": "pace_per_48"},  # fop0/fval0 missing: incomplete predicate
        _COMPETITION_FILTERABLE_KEYS,
        errors=errors,
    )
    assert filters == []
    assert len(errors) == 1

    errors.clear()
    filters = parse_metric_filters({}, _COMPETITION_FILTERABLE_KEYS, errors=errors)
    assert filters == []
    assert errors == []  # untouched slots are not errors


def test_parse_metric_filters_valid_and_invalid_rows_compose() -> None:
    """One invalid row never drops a sibling valid row (ticket #636)."""
    errors: list[str] = []
    filters = parse_metric_filters(
        {
            "fcol0": "pace_per_48",
            "fop0": "gte",
            "fval0": "90",
            "fcol1": "bogus_key",
            "fop1": "gte",
            "fval1": "1",
        },
        _COMPETITION_FILTERABLE_KEYS,
        errors=errors,
    )
    assert filters == [MetricFilter(col="pace_per_48", op=">=", value=90.0)]
    assert len(errors) == 1


def test_parse_query_competitions_malformed_year_min_visible_year_max_preserved() -> None:
    """A malformed year_min never erases a valid year_max (ticket #636)."""
    q = parse_query(
        {
            "subject": "competitions",
            "year_min": "not-a-year",
            "year_max": "2025",
        }
    )
    assert q.year_min is None
    assert q.year_max == 2025
    assert any("not-a-year" in msg for msg in q.validation_errors)


def test_parse_query_competitions_implausible_year_min_is_clamped() -> None:
    """An absurd year_min is clamped to the plausible floor, never dropped.

    _build_trend materializes one point per integer year in [year_min,
    year_max], so an unbounded value like -100000000 would otherwise make it
    allocate/iterate millions of TrendPoints before rendering (codex finding
    on PR #656). Dropping the value to None would remove the lower bound
    entirely and silently broaden the query — the exact anti-pattern #636
    forbids — so it must be clamped, keeping the filter restrictive.
    """
    q = parse_query(
        {
            "subject": "competitions",
            "year_min": "-100000000",
            "year_max": "2026",
        }
    )
    assert q.year_min == 2000
    assert q.year_max == 2026
    assert any("-100000000" in msg for msg in q.validation_errors)


def test_parse_query_competitions_far_future_year_max_is_clamped() -> None:
    """A year_max far beyond any plausible data is clamped, not dropped.

    Dropping it to None would unbound the upper end of the range and
    silently broaden results (#636's forbidden anti-pattern); clamping keeps
    it restrictive, e.g. a deliberately-empty ``year_min=2099`` query must
    stay empty rather than falling through to every competition.
    """
    from datetime import date

    q = parse_query({"subject": "competitions", "year_max": "9999999"})
    assert q.year_max == date.today().year + 1
    assert any("9999999" in msg for msg in q.validation_errors)


def test_parse_query_competitions_malformed_min_gp_recorded() -> None:
    q = parse_query({"subject": "competitions", "min_gp": "abc"})
    assert q.min_games == 0  # degrades to the competitions default, not silently
    assert any("abc" in msg for msg in q.validation_errors)


def test_parse_query_competitions_malformed_competition_id_recorded() -> None:
    q = parse_query(
        {
            "subject": "competitions",
            "profile_scope": "competition",
            "competition_id": "xyz",
        }
    )
    assert q.competition_id is None
    assert any("xyz" in msg for msg in q.validation_errors)


def test_parse_query_competitions_unknown_trend_metric_recorded() -> None:
    q = parse_query({"subject": "competitions", "trend_metric": "not_a_metric"})
    assert q.trend_metric is None
    assert any("not_a_metric" in msg for msg in q.validation_errors)


def test_parse_query_competitions_incomplete_predicate_recorded() -> None:
    """fcol0 set without fop0/fval0 is dropped with a visible note, not silently."""
    q = parse_query(
        {
            "subject": "competitions",
            "fcol0": "pace_per_48",
        }
    )
    assert q.metric_filters == []
    assert q.validation_errors  # not silent


def test_parse_query_players_subject_does_not_populate_metric_filter_errors() -> None:
    """Non-competitions subjects keep the pre-existing silent-degrade behavior
    for fcol/fop/fval (this ticket scopes validation surfacing to Competitions)."""
    q = parse_query({"subject": "players", "fcol0": "pts"})
    assert q.metric_filters == []
    assert q.validation_errors == []


def test_competition_columns_season_scope_has_no_venue_column() -> None:
    """A season profile pools every venue, so venue is not a per-row column."""
    cols = {c.key for c in competition_columns("season_all_competitions")}
    assert "venue" not in cols
    assert "year" in cols
    assert "pace_per_48" in cols


def test_competition_columns_competition_scope_has_venue_column() -> None:
    cols = {c.key for c in competition_columns("competition")}
    assert "venue" in cols


def test_competition_columns_only_registry_metrics_are_filterable() -> None:
    """Identity/meta columns (year, scope_key, ...) are never threshold-filterable —
    thresholds are restricted to registry-certified metrics (contract §6).
    """
    cols = competition_columns("competition")
    filterable = {c.key for c in cols if c.filterable}
    assert filterable == _COMPETITION_FILTERABLE_KEYS
    assert "year" not in filterable
    assert "scope_key" not in filterable


def test_build_profile_view_box_gated_metric_partial_when_undercovered() -> None:
    """box_complete_games < final_games -> partial coverage for a box-gated metric."""
    from app.services.summer_league_environment_registry import metrics_for_scope

    profile = _profile(final_games=20, box_complete_games=10)
    defs = metrics_for_scope("season_all_competitions")
    view = _build_profile_view(profile, defs)
    assert view.coverage["pace_per_48"].coverage == "partial"
    assert view.coverage["pace_per_48"].covered == 10
    assert view.coverage["pace_per_48"].eligible == 20


def test_build_profile_view_composition_share_computed_from_counts() -> None:
    """Composition shares (stored=False) are derived on read from count columns."""
    from app.services.summer_league_environment_registry import metrics_for_scope

    profile = _profile(rookie_count=40, appeared_players=100)
    defs = metrics_for_scope("season_all_competitions")
    view = _build_profile_view(profile, defs)
    assert view.raw_values["rookie_share"] == 0.4


def test_passes_coverage_filter_box_complete() -> None:
    from app.services.summer_league_environment_registry import metrics_for_scope

    complete = _build_profile_view(
        _profile(final_games=20, box_complete_games=20), metrics_for_scope("season_all_competitions")
    )
    partial = _build_profile_view(
        _profile(final_games=20, box_complete_games=5), metrics_for_scope("season_all_competitions")
    )
    assert _passes_coverage_filter(complete, "box_complete") is True
    assert _passes_coverage_filter(partial, "box_complete") is False
    assert _passes_coverage_filter(partial, "all") is True


def test_passes_metric_filter_rejects_null_and_partial_values() -> None:
    """Thresholds never fire on a null or partial-coverage metric (contract §6)."""
    from app.services.summer_league_environment_registry import metrics_for_scope

    defs = metrics_for_scope("season_all_competitions")
    definition = get_metric("pace_per_48")

    partial_view = _build_profile_view(
        _profile(final_games=20, box_complete_games=5, pace_per_48=None), defs
    )
    f = MetricFilter(col="pace_per_48", op=">=", value=50.0)
    assert _passes_metric_filter(partial_view, definition, f) is False

    complete_view = _build_profile_view(
        _profile(final_games=20, box_complete_games=20, pace_per_48=95.5), defs
    )
    assert _passes_metric_filter(complete_view, definition, f) is True
    assert (
        _passes_metric_filter(
            complete_view, definition, MetricFilter(col="pace_per_48", op="<=", value=50.0)
        )
        is False
    )


def test_team_count_is_filterable_via_generic_fcol_contract() -> None:
    """Team count (#640) is a registered registry metric reachable through the
    existing fcol/fop/fval contract -- not a one-off team_count= param."""
    assert "distinct_teams" in _COMPETITION_FILTERABLE_KEYS
    filters = parse_metric_filters(
        {"fcol0": "distinct_teams", "fop0": "gte", "fval0": "8"},
        _COMPETITION_FILTERABLE_KEYS,
    )
    assert filters == [MetricFilter(col="distinct_teams", op=">=", value=8.0)]


def test_team_count_column_is_filterable_and_sortable_in_competition_columns() -> None:
    cols = {c.key: c for c in competition_columns("competition")}
    assert cols["distinct_teams"].filterable is True
    assert cols["distinct_teams"].sortable is True
    assert cols["distinct_teams"].fmt == "int"


def test_passes_metric_filter_team_count_rejects_partial_box_coverage() -> None:
    """A team-count threshold never fires on a box-partial profile, even though
    distinct_teams itself is a non-nullable column (never widens results)."""
    from app.services.summer_league_environment_registry import metrics_for_scope

    defs = metrics_for_scope("season_all_competitions")
    definition = get_metric("distinct_teams")
    f = MetricFilter(col="distinct_teams", op=">=", value=1.0)

    partial_view = _build_profile_view(
        _profile(final_games=20, box_complete_games=5, distinct_teams=8), defs
    )
    assert _passes_metric_filter(partial_view, definition, f) is False

    complete_view = _build_profile_view(
        _profile(final_games=20, box_complete_games=20, distinct_teams=8), defs
    )
    assert _passes_metric_filter(complete_view, definition, f) is True
    assert (
        _passes_metric_filter(
            complete_view, definition, MetricFilter(col="distinct_teams", op=">=", value=9.0)
        )
        is False
    )


def test_view_to_row_carries_team_count_unscaled() -> None:
    """Team count is a plain count (scale=1.0), unlike ratio metrics."""
    from app.services.summer_league_environment_registry import metrics_for_scope

    defs = metrics_for_scope("season_all_competitions")
    metric_by_key = {d.key: d for d in defs}
    view = _build_profile_view(
        _profile(final_games=20, box_complete_games=20, distinct_teams=10), defs
    )
    row = _view_to_row(view, metric_by_key)
    assert row.values["distinct_teams"] == 10.0


def test_sort_competition_views_nulls_last_both_directions() -> None:
    from app.services.summer_league_environment_registry import metrics_for_scope

    defs = metrics_for_scope("season_all_competitions")
    metric_by_key = {d.key: d for d in defs}
    high = _build_profile_view(
        _profile(scope_key="season:2024", year=2024, final_games=20, box_complete_games=20, pace_per_48=100.0),
        defs,
    )
    low = _build_profile_view(
        _profile(scope_key="season:2023", year=2023, final_games=20, box_complete_games=20, pace_per_48=80.0),
        defs,
    )
    null_cov = _build_profile_view(
        _profile(scope_key="season:2022", year=2022, final_games=20, box_complete_games=0, pace_per_48=None),
        defs,
    )
    views = [null_cov, low, high]

    desc = _sort_competition_views(views, "pace_per_48", "desc", metric_by_key)
    assert [v.year for v in desc] == [2024, 2023, 2022]

    asc = _sort_competition_views(views, "pace_per_48", "asc", metric_by_key)
    assert [v.year for v in asc] == [2023, 2024, 2022]


def test_build_trend_has_visible_gaps_for_uncertified_years() -> None:
    """One point per surviving year; a non-complete year is a None gap, not a zero."""
    from app.services.summer_league_environment_registry import metrics_for_scope

    defs = metrics_for_scope("season_all_competitions")
    definition = get_metric("pace_per_48")
    covered = _build_profile_view(
        _profile(scope_key="season:2024", year=2024, final_games=20, box_complete_games=20, pace_per_48=95.5),
        defs,
    )
    uncovered = _build_profile_view(
        _profile(scope_key="season:2023", year=2023, final_games=20, box_complete_games=0, pace_per_48=None),
        defs,
    )
    trend = _build_trend(
        "pace_per_48", definition, [uncovered, covered], scope_kind="season_all_competitions", venue_slug=None
    )
    assert [p.year for p in trend.points] == [2023, 2024]
    assert trend.points[0].value is None
    assert trend.points[0].coverage == "unavailable"
    assert trend.points[1].value == 95.5
    assert trend.points[1].coverage == "complete"


def test_view_to_row_scales_ratio_metrics_for_display() -> None:
    """three_attempt_share is stored as a 0-1 ratio; the row value is display-scaled."""
    from app.services.summer_league_environment_registry import metrics_for_scope

    defs = metrics_for_scope("season_all_competitions")
    metric_by_key = {d.key: d for d in defs}
    view = _build_profile_view(
        _profile(final_games=20, box_complete_games=20, three_attempt_share=0.35), defs
    )
    row = _view_to_row(view, metric_by_key)
    assert row.values["three_attempt_share"] == 35.0
    assert row.href == "/stats/summer-league/2024"


def test_view_to_detail_is_stale_past_threshold() -> None:
    from app.services.summer_league_environment_registry import metrics_for_scope

    defs = metrics_for_scope("season_all_competitions")
    metric_by_key = {d.key: d for d in defs}
    stale_profile = _profile(
        calculated_at=datetime.utcnow() - timedelta(hours=STALE_AFTER_HOURS + 1)
    )
    fresh_profile = _profile(calculated_at=datetime.utcnow())
    stale_view = _build_profile_view(stale_profile, defs)
    fresh_view = _build_profile_view(fresh_profile, defs)

    stale_detail = _view_to_detail(stale_view, metric_by_key, [], [], [])
    fresh_detail = _view_to_detail(fresh_view, metric_by_key, [], [], [])
    assert stale_detail.is_stale is True
    assert fresh_detail.is_stale is False
    # The five-section metric grouping is populated for the detail panel.
    assert {s.key for s in fresh_detail.sections} == {
        "environment",
        "landscape",
        "composition",
    }
