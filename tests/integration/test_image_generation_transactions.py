"""Integration tests for image generation transaction semantics."""

from __future__ import annotations

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.fields import CohortType
from app.schemas.image_snapshots import (
    BatchJobState,
    ImageBatchJob,
    PlayerImageAsset,
    PlayerImageSnapshot,
)
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
    def __init__(self, job: object) -> None:
        self._job = job

    def get(self, *, name: str) -> object:  # noqa: ARG002
        return self._job


class _DummyClient:
    def __init__(self, job: object) -> None:
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
    def __init__(self, job: object, *, created_name: str) -> None:
        super().__init__(job)
        self._created = _DummyCreatedBatch(created_name)

    def create(self, *, model: str, src, config):  # noqa: ANN001, ARG002
        return self._created


class _DummyClientWithCreate(_DummyClient):
    def __init__(self, job: object, *, created_name: str) -> None:
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


class _DummyDest:
    def __init__(self, inlined_responses: list[object]) -> None:
        self.inlined_responses = inlined_responses


class _DummyBatchJobWithDest:
    def __init__(self, *, inlined_responses: list[object]) -> None:
        self.dest = _DummyDest(inlined_responses)
        self.error = None


class _DummyGenerateContentResponse:
    def __init__(self, *, text: str | None) -> None:
        self.text = text
        self.candidates: list[object] = []


class _DummyInlinedResponse:
    def __init__(
        self,
        *,
        error: _DummyError | None = None,
        text: str | None = None,
    ) -> None:
        self.error = error
        self.response = (
            _DummyGenerateContentResponse(text=text) if text is not None else None
        )


@pytest.mark.asyncio
async def test_retrieve_batch_results_ingests_successes_when_some_requests_error(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    test_schema: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-request errors should not block ingesting successful responses."""
    player_ok = PlayerMaster(display_name="Ok Player")
    player_err = PlayerMaster(display_name="Err Player")
    db_session.add_all([player_ok, player_err])
    await db_session.commit()
    assert player_ok.id is not None
    assert player_err.id is not None

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

    ok_request_id = "ok123"
    err_request_id = "err123"
    job = ImageBatchJob(
        gemini_job_name="batches/test",
        state=BatchJobState.running,
        snapshot_id=snapshot.id,
        player_ids_json=(
            f'[{{"player_id": {player_ok.id}, "slug": "ok", "dg_request_id": "{ok_request_id}" }},'
            f'{{"player_id": {player_err.id}, "slug": "err", "dg_request_id": "{err_request_id}" }}]'
        ),
        style="default",
        image_size="1K",
        total_requests=2,
    )
    db_session.add(job)
    await db_session.commit()
    assert job.id is not None

    dummy_job = _DummyBatchJobWithDest(
        inlined_responses=[
            _DummyInlinedResponse(error=_DummyError("boom")),
            _DummyInlinedResponse(text=f'{{"dg_request_id": "{ok_request_id}"}}'),
        ]
    )
    monkeypatch.setattr(image_generation_service, "_client", _DummyClient(dummy_job))
    monkeypatch.setattr(
        image_generation_service,
        "get_batch_job_status",
        lambda _name: BatchJobState.succeeded,
    )

    async def _fake_process_batch_response(  # type: ignore[override]
        *,
        db: AsyncSession,
        response: object,  # noqa: ARG001
        player: PlayerMaster,
        snapshot: PlayerImageSnapshot,
        style: str,  # noqa: ARG001
        dg_request_id: str | None = None,  # noqa: ARG001
    ):
        assert snapshot.id is not None
        assert player.id is not None
        return PlayerImageAsset(
            snapshot_id=snapshot.id,
            player_id=player.id,
            s3_key=f"players/{player.id}_{player.slug or player.id}_default.png",
            s3_bucket="test",
            public_url="https://example.test/image.png",
            user_prompt="test",
            error_message=None,
        )

    monkeypatch.setattr(
        image_generation_service,
        "_process_batch_response",
        _fake_process_batch_response,
    )

    success_count, failure_count = await image_generation_service.retrieve_batch_results(
        db=db_session,
        job_record=job,
        players_by_id={player_ok.id: player_ok, player_err.id: player_err},
        snapshot=snapshot,
    )
    assert success_count == 1
    assert failure_count == 1

    async with session_factory() as verify_session:
        await verify_session.execute(text(f'SET search_path TO "{test_schema}"'))
        result = await verify_session.execute(
            select(ImageBatchJob).where(ImageBatchJob.id == job.id)  # type: ignore[arg-type]
        )
        persisted = result.scalar_one()

    assert persisted.state == BatchJobState.succeeded
    assert persisted.completed_at is not None
    assert persisted.success_count == 1
    assert persisted.failure_count == 1
    assert persisted.error_message is not None
