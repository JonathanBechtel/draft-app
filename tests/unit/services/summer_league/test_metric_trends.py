"""Unit coverage for daily-close trend selection and response shaping."""

from __future__ import annotations

import re
from datetime import date, datetime
from unittest.mock import AsyncMock

import pytest

from app.domain.temporal import Watermark
from app.models.summer_league_trends import TrendCohortBand, TrendPoint
from app.services.sources.summer_league.metric_trends import (
    _effective_day_expression,
    get_daily_trend,
    latest_trend_watermark,
)


class _MappingsResult:
    """Minimal SQLAlchemy result facade for service-level query tests."""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def mappings(self) -> "_MappingsResult":
        return self

    def all(self) -> list[dict[str, object]]:
        return self.rows


@pytest.mark.asyncio
async def test_trend_shapes_player_values_and_cohort_band_in_day_order() -> None:
    """Player points use the selected row while the band uses its daily cohort."""
    db = AsyncMock()
    db.execute.return_value = _MappingsResult(
        [
            {
                "effective_day": date(2026, 7, 2),
                "player_id": 1,
                "as_of": datetime(2026, 8, 1, 11),
                "gmsc": 4.0,
                "ts_pct": 45.0,
                "trend_competition_bands": {
                    "gmsc": {"median": 6.0, "q1": 5.0, "q3": 7.0},
                    "ts_pct": {"median": 50.0, "q1": 47.5, "q3": 52.5},
                },
            },
            {
                "effective_day": date(2026, 7, 1),
                "player_id": 1,
                "as_of": datetime(2026, 8, 1, 10),
                "gmsc": 3.0,
                "ts_pct": 40.0,
                "trend_competition_bands": {
                    "gmsc": {"median": 3.0, "q1": 3.0, "q3": 3.0},
                    "ts_pct": {"median": 40.0, "q1": 40.0, "q3": 40.0},
                },
            },
        ]
    )

    points = await get_daily_trend(
        db,
        scope_key="competition:7",
        player_id=1,
        metric_keys=("gmsc", "ts_pct"),
    )

    assert [(point.effective_day, point.metric_key) for point in points] == [
        (date(2026, 7, 1), "gmsc"),
        (date(2026, 7, 1), "ts_pct"),
        (date(2026, 7, 2), "gmsc"),
        (date(2026, 7, 2), "ts_pct"),
    ]
    day_two_gmsc = points[2]
    assert day_two_gmsc.value == 4.0
    assert day_two_gmsc.cohort_band.median == 6.0
    assert day_two_gmsc.cohort_band.q1 == 5.0
    assert day_two_gmsc.cohort_band.q3 == 7.0


@pytest.mark.asyncio
async def test_unknown_metric_key_is_rejected_before_query() -> None:
    """The read contract accepts registry keys only, with a clear boundary error."""
    db = AsyncMock()

    with pytest.raises(ValueError, match="unknown registry metric key"):
        await get_daily_trend(
            db,
            scope_key="competition:7",
            player_id=None,
            metric_keys=("not_a_metric",),
        )

    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_daily_close_windows_never_rank_by_source_currency() -> None:
    """Both winner windows rank by event day + version, never by ``as_of``.

    The service documents that ``as_of`` supplies no ordering. Compiling the
    statement the service actually issues pins that for *both* ``row_number()``
    windows -- the per-competition/day winner and the per-day scope
    representative -- which reading ``_effective_day_expression()`` alone cannot.
    """
    db = AsyncMock()
    db.execute.return_value = _MappingsResult([])

    await get_daily_trend(
        db,
        scope_key="season:2026",
        player_id=None,
        metric_keys=("gmsc",),
    )

    statement = db.execute.await_args.args[0]
    sql = str(statement.compile(compile_kwargs={"literal_binds": True}))
    windows = re.findall(r"row_number\(\) OVER \(([^)]*)\)", sql)
    assert len(windows) == 2
    for window in windows:
        partition, _, order_by = window.partition("ORDER BY")
        assert "effective_day" in partition
        assert "version DESC" in order_by
        assert "is_archival DESC" in order_by
        # ``published_at`` is the visibility gate and final tie-breaker; source
        # currency must not appear in the ordering at all.
        assert "as_of" not in order_by


def test_effective_day_expression_does_not_use_publication_time() -> None:
    """Only explicit event evidence can supply the daily-close calendar key."""
    sql = str(
        _effective_day_expression().compile(compile_kwargs={"literal_binds": True})
    )

    assert "effective_day" in sql
    assert "published_at" not in sql


def _trend_point(day: date, as_of: datetime | None) -> TrendPoint:
    return TrendPoint(
        metric_key="gmsc",
        effective_day=day,
        value=1.0,
        cohort_band=TrendCohortBand(median=1.0, q1=0.0, q3=2.0),
        as_of=as_of,
    )


def test_latest_trend_watermark_empty_points_has_no_source_currency() -> None:
    """An empty trend series is a fully unknown watermark, never a bare None."""
    watermark = latest_trend_watermark([])

    assert watermark == Watermark(
        source_as_of=None, projection_built_at=None, projection_version=None
    )


def test_latest_trend_watermark_uses_newest_day_max_as_of() -> None:
    """The watermark's source currency is the newest day's max per-point as_of."""
    points = [
        _trend_point(date(2026, 7, 1), datetime(2026, 7, 1, 10)),
        _trend_point(date(2026, 7, 2), datetime(2026, 7, 2, 9)),
        _trend_point(date(2026, 7, 2), datetime(2026, 7, 2, 12)),
    ]

    watermark = latest_trend_watermark(points)

    assert watermark == Watermark(
        source_as_of=datetime(2026, 7, 2, 12),
        projection_built_at=None,
        projection_version=None,
    )


def test_latest_trend_watermark_unknown_currency_on_newest_day_is_none() -> None:
    """A missing as_of on the newest day degrades the whole watermark, not one point."""
    points = [
        _trend_point(date(2026, 7, 1), datetime(2026, 7, 1, 10)),
        _trend_point(date(2026, 7, 2), None),
    ]

    watermark = latest_trend_watermark(points)

    assert watermark == Watermark(
        source_as_of=None, projection_built_at=None, projection_version=None
    )
