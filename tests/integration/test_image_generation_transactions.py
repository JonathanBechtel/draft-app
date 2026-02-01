"""Integration tests for image generation transaction semantics."""

from __future__ import annotations

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.fields import CohortType
from app.schemas.image_snapshots import BatchJobState, ImageBatchJob, PlayerImageSnapshot
from app.schemas.players_master import PlayerMaster
from app.services.image_generation import image_generation_service


class _DummyError:
    def __init__(self, message: str) -> None:
        self.message = message


class _DummyBatchJob:
    def __init__(self, *, error_message: str | None) -> None:
        self.dest = None
        self.error = _DummyError(error_message) if error_message is not None else None


class _DummyBatches:
    def __init__(self, job: _DummyBatchJob) -> None:
        self._job = job

    def get(self, *, name: str) -> _DummyBatchJob:  # noqa: ARG002
        return self._job


class _DummyClient:
    def __init__(self, job: _DummyBatchJob) -> None:
        self.batches = _DummyBatches(job)


@pytest.mark.asyncio
async def test_retrieve_batch_results_persists_failure_metadata_before_raise(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    test_schema: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing inlined responses should still commit failure metadata before raising."""
    snapshot = PlayerImageSnapshot(
        run_key="test_run",
        version=1,
        is_current=False,
        style="default",
        cohort=CohortType.current_draft,
        image_size="1K",
        system_prompt="test system prompt",
    )
    db_session.add(snapshot)
    await db_session.commit()
    assert snapshot.id is not None

    job = ImageBatchJob(
        gemini_job_name="batches/test",
        state=BatchJobState.running,
        snapshot_id=snapshot.id,
        player_ids_json="[1,2]",
        style="default",
        image_size="1K",
        total_requests=2,
    )
    db_session.add(job)
    await db_session.commit()
    assert job.id is not None

    dummy_job = _DummyBatchJob(error_message="no inlined responses")
    monkeypatch.setattr(image_generation_service, "_client", _DummyClient(dummy_job))
    monkeypatch.setattr(
        image_generation_service,
        "get_batch_job_status",
        lambda _name: BatchJobState.failed,
    )

    with pytest.raises(RuntimeError, match="did not return inlined responses"):
        await image_generation_service.retrieve_batch_results(
            db=db_session,
            job_record=job,
            players_by_id={},
            snapshot=snapshot,
        )

    async with session_factory() as verify_session:
        await verify_session.execute(text(f'SET search_path TO "{test_schema}"'))
        result = await verify_session.execute(
            select(ImageBatchJob).where(ImageBatchJob.id == job.id)  # type: ignore[arg-type]
        )
        persisted = result.scalar_one()

    assert persisted.state == BatchJobState.failed
    assert persisted.completed_at is not None
    assert persisted.success_count == 0
    assert persisted.failure_count == job.total_requests
    assert persisted.error_message == "no inlined responses"


class _DummyCreatedBatch:
    def __init__(self, name: str) -> None:
        self.name = name


class _DummyBatchesWithCreate(_DummyBatches):
    def __init__(self, job: _DummyBatchJob, *, created_name: str) -> None:
        super().__init__(job)
        self._created = _DummyCreatedBatch(created_name)

    def create(self, *, model: str, src, config):  # noqa: ANN001, ARG002
        return self._created


class _DummyClientWithCreate(_DummyClient):
    def __init__(self, job: _DummyBatchJob, *, created_name: str) -> None:
        self.batches = _DummyBatchesWithCreate(job, created_name=created_name)


@pytest.mark.asyncio
async def test_submit_batch_job_does_not_require_clean_transaction(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """submit_batch_job should work even after a prior SELECT began a tx."""
    player = PlayerMaster(display_name="Test Player")
    db_session.add(player)
    await db_session.commit()
    assert player.id is not None

    snapshot = PlayerImageSnapshot(
        run_key="test_run",
        version=1,
        is_current=False,
        style="default",
        cohort=CohortType.current_draft,
        image_size="1K",
        system_prompt="test system prompt",
    )
    db_session.add(snapshot)
    await db_session.commit()
    assert snapshot.id is not None

    # Force an implicit transaction to be active.
    await db_session.execute(select(1))

    dummy_job = _DummyBatchJob(error_message=None)
    monkeypatch.setattr(
        image_generation_service,
        "_client",
        _DummyClientWithCreate(dummy_job, created_name="batches/test-created"),
    )

    job_record = await image_generation_service.submit_batch_job(
        db=db_session,
        players=[player],
        snapshot=snapshot,
        style="default",
        image_size="1K",
        fetch_likeness=False,
    )
    await db_session.commit()

    assert job_record.id is not None
    assert job_record.gemini_job_name == "batches/test-created"
