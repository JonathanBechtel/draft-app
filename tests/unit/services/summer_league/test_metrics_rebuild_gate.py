"""Unit coverage for the metrics gate's dual publication stamps."""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import AsyncMock

import pytest

from app.services.summer_league import metrics_rebuild_gate


@pytest.mark.asyncio
async def test_publish_metric_summary_forwards_as_of_and_effective_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gate publication carries source currency and event day independently."""
    publish = AsyncMock(return_value=set())
    monkeypatch.setattr(metrics_rebuild_gate, "publish_metric_version", publish)
    db = AsyncMock()

    await metrics_rebuild_gate._publish_metric_summary(
        db,
        metrics_version=9,
        summary={
            "model_version": "fit-9",
            "as_of": datetime(2026, 8, 1, 12),
            "effective_day": date(2026, 7, 14),
        },
    )

    publish.assert_awaited_once_with(
        db,
        version=9,
        model_version="fit-9",
        as_of=datetime(2026, 8, 1, 12),
        effective_day=date(2026, 7, 14),
    )
