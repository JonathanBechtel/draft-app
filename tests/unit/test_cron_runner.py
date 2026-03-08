"""Unit tests for the scheduled content cron runner."""

from __future__ import annotations

import pytest

from app.cli import cron_runner
from app.models.news import IngestionResult
from app.models.podcasts import PodcastIngestionResult


class _DummySession:
    """Minimal async context manager used to stub SessionLocal()."""

    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


@pytest.mark.asyncio
async def test_main_runs_news_and_podcast_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cron runner executes both ingestion jobs and succeeds when neither raises."""
    calls: list[str] = []

    def _session_local() -> _DummySession:
        return _DummySession()

    async def _fake_news_ingestion(_: object) -> IngestionResult:
        calls.append("news")
        return IngestionResult(
            sources_processed=2,
            items_added=5,
            items_skipped=1,
            mentions_added=3,
            errors=[],
        )

    async def _fake_podcast_ingestion(_: object) -> PodcastIngestionResult:
        calls.append("podcasts")
        return PodcastIngestionResult(
            shows_processed=1,
            episodes_added=4,
            episodes_skipped=2,
            mentions_added=1,
            errors=[],
        )

    async def _fake_dispose_engine() -> None:
        calls.append("dispose")

    monkeypatch.setattr(cron_runner, "SessionLocal", _session_local)
    monkeypatch.setattr(cron_runner, "run_news_ingestion_cycle", _fake_news_ingestion)
    monkeypatch.setattr(
        cron_runner, "run_podcast_ingestion_cycle", _fake_podcast_ingestion
    )
    monkeypatch.setattr(cron_runner, "dispose_engine", _fake_dispose_engine)

    result = await cron_runner.main()

    assert result == 0
    assert calls == ["news", "podcasts", "dispose"]


@pytest.mark.asyncio
async def test_main_returns_failure_when_news_job_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cron runner still attempts podcasts and returns 1 when news ingestion fails."""
    calls: list[str] = []

    def _session_local() -> _DummySession:
        return _DummySession()

    async def _failing_news_ingestion(_: object) -> IngestionResult:
        calls.append("news")
        raise RuntimeError("news boom")

    async def _fake_podcast_ingestion(_: object) -> PodcastIngestionResult:
        calls.append("podcasts")
        return PodcastIngestionResult(
            shows_processed=1,
            episodes_added=0,
            episodes_skipped=0,
            mentions_added=0,
            errors=[],
        )

    async def _fake_dispose_engine() -> None:
        calls.append("dispose")

    monkeypatch.setattr(cron_runner, "SessionLocal", _session_local)
    monkeypatch.setattr(
        cron_runner, "run_news_ingestion_cycle", _failing_news_ingestion
    )
    monkeypatch.setattr(
        cron_runner, "run_podcast_ingestion_cycle", _fake_podcast_ingestion
    )
    monkeypatch.setattr(cron_runner, "dispose_engine", _fake_dispose_engine)

    result = await cron_runner.main()

    assert result == 1
    assert calls == ["news", "podcasts", "dispose"]
