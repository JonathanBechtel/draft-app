"""Unit tests for the scheduled content cron runner."""

from __future__ import annotations

import pytest

from app.cli import cron_runner
from app.models.news import IngestionResult
from app.models.podcasts import PodcastIngestionResult
from app.models.videos import VideoIngestionResult


class _DummySession:
    """Minimal async context manager used to stub SessionLocal()."""

    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


def _make_stubs(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    """Wire up common monkeypatches for all three ingestion jobs."""

    def _session_local() -> _DummySession:
        return _DummySession()

    async def _fake_news(_: object) -> IngestionResult:
        calls.append("news")
        return IngestionResult(
            sources_processed=2, items_added=5, items_skipped=1,
            mentions_added=3, errors=[],
        )

    async def _fake_podcast(_: object) -> PodcastIngestionResult:
        calls.append("podcasts")
        return PodcastIngestionResult(
            shows_processed=1, episodes_added=4, episodes_skipped=2,
            mentions_added=1, errors=[],
        )

    async def _fake_video(_: object) -> VideoIngestionResult:
        calls.append("videos")
        return VideoIngestionResult(
            channels_processed=1, videos_added=3, videos_skipped=1,
            errors=[],
        )

    async def _fake_dispose() -> None:
        calls.append("dispose")

    monkeypatch.setattr(cron_runner, "SessionLocal", _session_local)
    monkeypatch.setattr(cron_runner, "run_news_ingestion_cycle", _fake_news)
    monkeypatch.setattr(cron_runner, "run_podcast_ingestion_cycle", _fake_podcast)
    monkeypatch.setattr(cron_runner, "run_video_ingestion_cycle", _fake_video)
    monkeypatch.setattr(cron_runner, "dispose_engine", _fake_dispose)


@pytest.mark.asyncio
async def test_main_runs_all_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cron runner executes all ingestion jobs and succeeds when none raise."""
    calls: list[str] = []
    _make_stubs(monkeypatch, calls)

    result = await cron_runner.main()

    assert result == 0
    assert calls == ["news", "podcasts", "videos", "dispose"]


@pytest.mark.asyncio
async def test_main_returns_failure_when_news_job_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cron runner still attempts podcasts and videos, returns 1 when news fails."""
    calls: list[str] = []
    _make_stubs(monkeypatch, calls)

    async def _failing_news(_: object) -> IngestionResult:
        calls.clear()
        calls.append("news")
        raise RuntimeError("news boom")

    monkeypatch.setattr(cron_runner, "run_news_ingestion_cycle", _failing_news)

    result = await cron_runner.main()

    assert result == 1
    assert calls == ["news", "podcasts", "videos", "dispose"]


@pytest.mark.asyncio
async def test_main_returns_failure_when_video_job_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cron runner still completes news/podcasts, returns 1 when videos fail."""
    calls: list[str] = []
    _make_stubs(monkeypatch, calls)

    async def _failing_video(_: object) -> VideoIngestionResult:
        calls.append("videos_fail")
        raise RuntimeError("video boom")

    monkeypatch.setattr(cron_runner, "run_video_ingestion_cycle", _failing_video)

    result = await cron_runner.main()

    assert result == 1
    assert calls == ["news", "podcasts", "videos_fail", "dispose"]
