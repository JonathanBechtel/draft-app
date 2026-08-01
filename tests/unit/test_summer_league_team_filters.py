"""Unit tests for the Explorer team's filter/display parity contract."""

from __future__ import annotations

from app.services.summer_league_explorer_service import (
    ExplorerRow,
    MetricFilter,
    _TEAM_FILTERABLE_KEYS,
    _filter_team_rows,
    parse_metric_filters,
    parse_query,
)


def test_team_filter_vocabulary_matches_displayed_advanced_columns() -> None:
    """Team filters expose ratings and pace, not player-only metrics."""
    assert _TEAM_FILTERABLE_KEYS == {"pace", "ortg", "drtg", "net_rtg"}
    assert parse_metric_filters(
        {"fcol0": "net_rtg", "fop0": "gte", "fval0": "5"},
        _TEAM_FILTERABLE_KEYS,
    ) == [MetricFilter(col="net_rtg", op=">=", value=5.0)]
    assert (
        parse_query(
            {"subject": "teams", "fcol0": "ts_pct", "fop0": "gte", "fval0": "50"}
        ).metric_filters
        == []
    )


def test_team_filter_uses_the_same_net_rating_value_as_display() -> None:
    """A NetRtg threshold excludes rows based on their displayed value."""
    rows = [
        ExplorerRow(label="Alpha", href="/alpha", values={"net_rtg": 5.0}),
        ExplorerRow(label="Bravo", href="/bravo", values={"net_rtg": -2.0}),
    ]
    assert [
        row.label
        for row in _filter_team_rows(
            rows, [MetricFilter(col="net_rtg", op=">=", value=5.1)]
        )
    ] == []
    assert [
        row.label
        for row in _filter_team_rows(
            rows, [MetricFilter(col="net_rtg", op=">=", value=0.0)]
        )
    ] == ["Alpha"]
