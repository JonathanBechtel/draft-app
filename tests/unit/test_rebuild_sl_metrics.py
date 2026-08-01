"""Tests for the full Summer League metrics rebuild command."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.event_desk import snapshot_materialization
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
        snapshot_materialization, "build_desk_render_variants", build_variants
    )
    monkeypatch.setattr(
        snapshot_materialization, "upsert_render_snapshots", upsert_snapshots
    )

    db = AsyncMock()
    refreshed = await snapshot_materialization.materialize_desk_render_snapshots(db)

    assert refreshed == 2
    build_variants.assert_awaited_once_with(db, scheduled_write=True)
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
        snapshot_materialization, "build_desk_render_variants", build_variants
    )
    monkeypatch.setattr(
        snapshot_materialization, "upsert_render_snapshots", upsert_snapshots
    )

    db = AsyncMock()
    refreshed = await snapshot_materialization.materialize_desk_render_snapshots(db)

    assert refreshed == 0
    build_variants.assert_awaited_once_with(db, scheduled_write=True)
    upsert_snapshots.assert_not_awaited()


class _FakeBegin:
    """Async context manager standing in for ``AsyncSession.begin()``."""

    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class _FakeSession:
    """Minimal async session exposing ``begin()`` as a context manager."""

    def begin(self) -> _FakeBegin:
        return _FakeBegin()


class _FakeSessionLocal:
    """Async context manager standing in for ``SessionLocal()``."""

    async def __aenter__(self) -> _FakeSession:
        return _FakeSession()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


@pytest.mark.asyncio
async def test_main_acquires_bounded_writer_lock_before_publishing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The manual rebuild script takes the bounded lock, not the unbounded one.

    A manual run racing a scheduled cron writer previously used the
    unbounded blocking acquire and could hang indefinitely; the bounded
    acquire fails loudly with a timeout instead, matching the Desk tick
    classes' pattern. See #719 item 10.
    """
    lock_calls: list[dict[str, object]] = []

    async def _fake_set_repeatable_read(_db: object) -> None:
        return None

    async def _fake_rebuild_staged(_db: object) -> dict[str, object]:
        return {
            "seasons": 1,
            "contexts": 1,
            "adv_pools": 1,
            "version": 7,
            "model_version": "fake-model",
        }

    async def _fake_prepare_snapshots(
        _db: object, *, metrics_version: int
    ) -> list[object]:
        assert metrics_version == 7
        return []

    async def _fake_acquire_bounded(
        _db: object, *, max_wait_seconds: float
    ) -> None:
        lock_calls.append({"max_wait_seconds": max_wait_seconds})

    async def _fake_publish(_db: object, *, version: int, model_version: str) -> set[int]:
        assert version == 7
        assert model_version == "fake-model"
        return set()

    async def _fake_upsert(_db: object, _writes: object) -> None:
        return None

    class _FakeEngine:
        async def dispose(self) -> None:
            return None

    monkeypatch.setattr(rebuild_sl_metrics, "SessionLocal", lambda: _FakeSessionLocal())
    monkeypatch.setattr(
        rebuild_sl_metrics, "set_repeatable_read_snapshot", _fake_set_repeatable_read
    )
    monkeypatch.setattr(rebuild_sl_metrics, "rebuild_staged", _fake_rebuild_staged)
    monkeypatch.setattr(
        rebuild_sl_metrics, "prepare_desk_render_snapshots", _fake_prepare_snapshots
    )
    monkeypatch.setattr(
        rebuild_sl_metrics,
        "acquire_summer_league_writer_lock_bounded",
        _fake_acquire_bounded,
    )
    monkeypatch.setattr(rebuild_sl_metrics, "publish_metric_version", _fake_publish)
    monkeypatch.setattr(rebuild_sl_metrics, "upsert_render_snapshots", _fake_upsert)
    monkeypatch.setattr(rebuild_sl_metrics, "engine", _FakeEngine())

    await rebuild_sl_metrics.main()

    assert lock_calls == [
        {"max_wait_seconds": rebuild_sl_metrics.DEFAULT_REBUILD_LOCK_MAX_WAIT_SECONDS}
    ]


@pytest.mark.asyncio
async def test_staged_snapshot_prepare_reads_candidate_metrics_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Candidate Desk variants can be built before the metric pointer flips."""
    build_variants = AsyncMock(return_value=None)
    monkeypatch.setattr(
        snapshot_materialization, "build_desk_render_variants", build_variants
    )

    db = AsyncMock()
    writes = await snapshot_materialization.prepare_desk_render_snapshots(
        db, metrics_version=9
    )

    assert writes == []
    build_variants.assert_awaited_once_with(db, scheduled_write=True, metrics_version=9)
