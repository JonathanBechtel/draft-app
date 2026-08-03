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


@pytest.mark.asyncio
async def test_publish_metric_summary_omits_stamps_it_does_not_have(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Older rebuild shims omit the optional stamps instead of publishing ``None``.

    A summary may carry an explicit ``None`` (``as_of`` here) or leave the key
    out entirely (``effective_day``). Both take the compatibility branch, which
    drops the keyword rather than forwarding a null that would blank the column
    on every promoted row. The publisher's skipped-scope result is passed
    through unchanged.
    """
    publish = AsyncMock(return_value={4})
    monkeypatch.setattr(metrics_rebuild_gate, "publish_metric_version", publish)
    db = AsyncMock()

    skipped = await metrics_rebuild_gate._publish_metric_summary(
        db,
        metrics_version=3,
        summary={"model_version": "fit-3", "as_of": None},
    )

    assert skipped == {4}
    publish.assert_awaited_once_with(db, version=3, model_version="fit-3")
