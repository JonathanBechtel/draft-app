"""Unit coverage for daily-close trend selection and response shaping."""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import AsyncMock

import pytest

from app.services.summer_league.metric_trends import (
    _legacy_effective_day,
    get_daily_trend,
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
                "player_id": 2,
                "as_of": datetime(2026, 8, 1, 12),
                "gmsc": 8.0,
                "ts_pct": 55.0,
            },
            {
                "effective_day": date(2026, 7, 2),
                "player_id": 1,
                "as_of": datetime(2026, 8, 1, 11),
                "gmsc": 4.0,
                "ts_pct": 45.0,
            },
            {
                "effective_day": date(2026, 7, 1),
                "player_id": 1,
                "as_of": datetime(2026, 8, 1, 10),
                "gmsc": 3.0,
                "ts_pct": 40.0,
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


def test_legacy_fallback_uses_published_at_in_eastern_timezone() -> None:
    """NULL effective-day fallback cannot accidentally order by source currency."""
    sql = str(_legacy_effective_day().compile(compile_kwargs={"literal_binds": True}))

    assert "published_at" in sql
    assert "America/New_York" in sql
