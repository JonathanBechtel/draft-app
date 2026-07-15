"""Tests for the full Summer League metrics rebuild command."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from scripts import rebuild_sl_metrics


@pytest.mark.asyncio
async def test_full_metrics_rebuild_refreshes_all_desk_snapshot_variants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A metrics rebuild replaces the cached tracker variants in the same transaction."""
    freshness = SimpleNamespace(
        last_tick_at=datetime(2026, 7, 15, 16, 0),
        next_tick_eta=datetime(2026, 7, 15, 17, 0),
    )
    view = SimpleNamespace(payload=SimpleNamespace(freshness=freshness))
    variants = [
        SimpleNamespace(
            daily_state="preview",
            tracker_cohort="full_class",
            tracker_stat_view="advanced",
            view=view,
        ),
        SimpleNamespace(
            daily_state="live",
            tracker_cohort="lottery",
            tracker_stat_view="box",
            view=view,
        ),
    ]
    build_variants = AsyncMock(return_value=(42, variants))
    upsert_snapshots = AsyncMock()
    monkeypatch.setattr(
        rebuild_sl_metrics, "build_desk_render_variants", build_variants
    )
    monkeypatch.setattr(rebuild_sl_metrics, "upsert_render_snapshots", upsert_snapshots)

    db = AsyncMock()
    refreshed = await rebuild_sl_metrics._materialize_desk_render_snapshots(db)

    assert refreshed == 2
    build_variants.assert_awaited_once_with(db)
    upsert_snapshots.assert_awaited_once()
    await_args = upsert_snapshots.await_args
    assert await_args is not None
    writes = await_args.args[1]
    assert [(write.event_id, write.tracker_stat_view) for write in writes] == [
        (42, "advanced"),
        (42, "box"),
    ]


@pytest.mark.asyncio
async def test_full_metrics_rebuild_skips_desk_snapshot_refresh_off_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Off-window full rebuilds remain safe no-ops for the homepage cache."""
    build_variants = AsyncMock(return_value=None)
    upsert_snapshots = AsyncMock()
    monkeypatch.setattr(
        rebuild_sl_metrics, "build_desk_render_variants", build_variants
    )
    monkeypatch.setattr(rebuild_sl_metrics, "upsert_render_snapshots", upsert_snapshots)

    refreshed = await rebuild_sl_metrics._materialize_desk_render_snapshots(AsyncMock())

    assert refreshed == 0
    upsert_snapshots.assert_not_awaited()
