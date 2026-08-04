"""Pure contract tests for the cumulative trend presentation adapter."""

from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.summer_league_trends import TrendCohortBand, TrendPoint
from app.services.sources.summer_league.metric_trends import (
    resolve_scope_label,
    trend_points_to_context,
)


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
    assert payload["latest_as_of_label"] == "2026-07-20 12:00 UTC"
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


def test_trend_context_preserves_unknown_freshness_on_newest_day() -> None:
    """An unknown newest-close watermark is not replaced by an older timestamp."""
    band = TrendCohortBand(median=7.0, q1=5.0, q3=9.0)
    points = [
        TrendPoint(
            metric_key="gmsc",
            effective_day=date(2024, 7, 10),
            value=8.0,
            cohort_band=band,
            as_of=datetime(2026, 7, 20, 12),
        ),
        TrendPoint(
            metric_key="gmsc",
            effective_day=date(2024, 7, 11),
            value=9.0,
            cohort_band=band,
            as_of=None,
        ),
    ]

    payload = trend_points_to_context(
        points,
        scope_key="competition:42",
        scope_label="2024 trend",
        player_id=7,
    )

    assert payload is not None
    assert payload["latest_effective_day"] == "2024-07-11"
    assert payload["latest_as_of"] is None
    assert payload["latest_as_of_label"] == "not reported"


@pytest.mark.asyncio
async def test_season_scope_label_needs_no_competition_lookup() -> None:
    """A season scope is named from the key itself, without a database read."""
    db = AsyncMock()

    assert await resolve_scope_label(db, "season:2026") == "2026 Summer League"
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_competition_scope_label_falls_back_without_leaking_the_key() -> None:
    """An unresolvable competition still yields a label, never the raw key."""
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db.execute.return_value = result

    label = await resolve_scope_label(db, "competition:42")

    assert label == "Summer League"
    assert "42" not in label
