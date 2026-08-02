"""Pure contract tests for the cumulative trend presentation adapter."""

from datetime import date, datetime

from app.models.summer_league_trends import TrendCohortBand, TrendPoint
from app.services.summer_league.metric_trends import trend_points_to_context


def test_trend_context_keeps_event_day_and_source_currency_distinct() -> None:
    """The chart payload exposes both labels without treating ``as_of`` as a day key."""
    points = [
        TrendPoint(
            metric_key="gmsc",
            effective_day=date(2024, 7, 10),
            value=8.0,
            cohort_band=TrendCohortBand(median=7.0, q1=5.0, q3=9.0),
            as_of=datetime(2026, 7, 20, 12),
        ),
        TrendPoint(
            metric_key="ts_pct",
            effective_day=date(2024, 7, 10),
            value=0.6,
            cohort_band=TrendCohortBand(median=0.55, q1=0.5, q3=0.65),
            as_of=datetime(2026, 7, 20, 12),
        ),
    ]

    payload = trend_points_to_context(
        points,
        scope_key="competition:42",
        scope_label="2024 trend",
        player_id=7,
    )

    assert payload is not None
    assert payload["latest_effective_day"] == "2024-07-10"
    assert payload["latest_as_of"] == "2026-07-20T12:00:00"
    assert payload["player_id"] == 7
    assert payload["single_point"] is True


def test_trend_context_omits_empty_series() -> None:
    """No retained points means the page omits the module instead of an empty shell."""
    assert (
        trend_points_to_context(
            [], scope_key="competition:42", scope_label="2024 trend"
        )
        is None
    )
