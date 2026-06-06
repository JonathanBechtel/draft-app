"""Integration tests for enrichment_queue_service and the enrich_player refactor.

Exercises the full queue cycle against a real Postgres test schema.
All external calls (Gemini, Wikimedia, portrait/S3) are mocked — no
real network traffic is made.

Covered behaviors:
1. Refactor parity: run_enrichment_sweep still enriches unattempted stubs.
2. enrich_player fills empty bio fields, upserts college stats, stamps
   enrichment_attempted_at; re-running is idempotent (no clobber).
3. enqueue_enrichment creates job rows; dedup skips in-flight players;
   cap is enforced.
4. drain_enrichment_queue transitions queued→running→succeeded/failed;
   stamps enrichment_attempted_at; reclaims stale running rows.
5. enrichment_status returns {player_id: JobStatus} reflecting latest job.
6. Cron backstop wiring: _run_drain_queue_job is called in cron_runner.main().

Requires TEST_DATABASE_URL and PYTEST_ALLOW_DB=1.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.schemas.player_enrichment_jobs import PlayerEnrichmentJob
from app.schemas.player_lifecycle import PlayerLifecycle
from app.schemas.player_status import PlayerStatus
from app.schemas.players_master import PlayerMaster
from app.services.enrichment_queue_service import (
    MAX_ENQUEUE_PER_REQUEST,
    DrainResult,
    JobStatus,
    drain_enrichment_queue,
    enqueue_enrichment,
    enrichment_status,
)


# ---------------------------------------------------------------------------
# Schema-aware session factory fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def schema_session_factory(
    async_engine: Any,
    test_schema: str,
) -> Any:
    """Return a session factory whose sessions always use the test search_path.

    ``enrich_player`` and ``drain_enrichment_queue`` open their own sessions
    internally.  The base ``session_factory`` fixture's connections may not
    have the test-schema search_path set.  This fixture registers a
    ``do_connect`` listener on the engine so that *every* connection from the
    pool executes ``SET search_path`` before being handed to SQLAlchemy,
    without touching the session-level transaction state.
    """
    from sqlalchemy import event
    from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

    schema = test_schema

    # Register a connect-level event so every new asyncpg connection from the
    # pool gets the right search_path before SQLAlchemy opens a transaction.
    # The listener is removed after the test via a finalizer.
    @event.listens_for(async_engine.sync_engine, "connect")
    def _set_pg_search_path(dbapi_conn: Any, connection_record: Any) -> None:
        """Set the test schema as the default search_path on each new connection."""
        # dbapi_conn is a asyncpg AdaptedConnection wrapper.
        # We schedule the SET via the connection's existing synchronous API.
        # asyncpg does not support running coroutines from a sync listener,
        # so we store the schema name in the connection info and apply it
        # via a do_execute event instead.
        connection_record.info["test_schema"] = schema

    @event.listens_for(async_engine.sync_engine, "begin")
    def _apply_search_path(conn: Any) -> None:
        """Apply SET search_path at the start of each transaction."""
        test_schema_name = conn.info.get("test_schema")
        if test_schema_name:
            conn.exec_driver_sql(f'SET search_path TO "{test_schema_name}"')

    factory = async_sessionmaker(
        bind=async_engine,
        expire_on_commit=False,
        class_=AsyncSession,
    )

    yield factory

    # Clean up the event listeners
    event.remove(async_engine.sync_engine, "connect", _set_pg_search_path)
    event.remove(async_engine.sync_engine, "begin", _apply_search_path)


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _stub_player(
    db: AsyncSession,
    display_name: str,
    enrichment_attempted_at: datetime | None = None,
) -> PlayerMaster:
    """Create and flush a minimal stub PlayerMaster."""
    player = PlayerMaster(
        first_name=display_name.split()[0],
        last_name=" ".join(display_name.split()[1:]) or "X",
        display_name=display_name,
        draft_year=2026,
        is_stub=True,
        enrichment_attempted_at=enrichment_attempted_at,
    )
    db.add(player)
    await db.flush()
    return player


# Mock data returned by the Gemini/Wikimedia stages
_MOCK_BIO_DATA = {
    "confidence": "high",
    "birthdate": "2004-06-01",
    "birth_city": "Atlanta",
    "birth_state_province": "GA",
    "birth_country": "USA",
    "school": "Duke",
    "high_school": None,
    "shoots": "Right",
    "draft_year": 2026,
    "rsci_rank": 5,
    "height_inches": 78,
    "weight_lbs": 220,
    "position": "SF",
    "likeness_description": None,
    "season": "2024-25",
    "stats": {
        "games": 32,
        "games_started": 32,
        "mpg": 33.0,
        "ppg": 18.5,
        "rpg": 7.2,
        "apg": 3.1,
        "spg": 1.2,
        "bpg": 0.8,
        "fg_pct": 47.3,
        "three_p_pct": 36.0,
        "three_pa": 4.2,
        "ft_pct": 78.0,
        "fta": 4.5,
        "tov": 2.0,
        "pf": 1.9,
    },
}

_MOCK_REFERENCE_IMAGE = "https://upload.wikimedia.org/test_player.jpg"


# ---------------------------------------------------------------------------
# Patch targets
# ---------------------------------------------------------------------------

PATCH_FETCH_BIO = "app.services.player_enrichment_service._fetch_bio_and_stats"
PATCH_FIND_IMAGE = "app.services.player_enrichment_service._find_reference_image"
PATCH_GENERATE_PORTRAIT = "app.services.player_enrichment_service._generate_portrait"
PATCH_GEMINI_CLIENT = "app.services.player_enrichment_service.genai.Client"
PATCH_GEMINI_CLIENT_QUEUE = "app.services.enrichment_queue_service.genai.Client"


# ---------------------------------------------------------------------------
# 1. Refactor parity: run_enrichment_sweep still enriches unattempted stubs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_enriches_unattempted_stubs(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    """run_enrichment_sweep finds and enriches stubs with no enrichment_attempted_at.

    External calls (Gemini, Wikimedia) are mocked.  After the sweep the
    player should have bio fields set and enrichment_attempted_at stamped.
    """
    player = await _stub_player(db_session, "Jamal Rivers")
    await db_session.commit()
    player_id = player.id

    with (
        patch(PATCH_FETCH_BIO, new_callable=AsyncMock, return_value=_MOCK_BIO_DATA),
        patch(PATCH_FIND_IMAGE, new_callable=AsyncMock, return_value=_MOCK_REFERENCE_IMAGE),
        patch(PATCH_GENERATE_PORTRAIT, new_callable=AsyncMock),
        patch(PATCH_GEMINI_CLIENT) as mock_client_cls,
    ):
        mock_client_cls.return_value = MagicMock()

        from app.services.player_enrichment_service import run_enrichment_sweep

        result = await run_enrichment_sweep(session_factory)

    assert result.players_attempted >= 1
    assert result.players_enriched >= 1
    assert result.players_failed == 0

    # Verify DB state
    async with session_factory() as verify_db:
        updated = await verify_db.get(PlayerMaster, player_id)
        assert updated is not None
        assert updated.enrichment_attempted_at is not None
        assert updated.school == "Duke"
        assert updated.birth_city == "Atlanta"


@pytest.mark.asyncio
async def test_sweep_skips_already_attempted(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    """run_enrichment_sweep does not re-enrich players with enrichment_attempted_at set."""
    player = await _stub_player(db_session, "Taylor Green", enrichment_attempted_at=_now())
    await db_session.commit()

    with (
        patch(PATCH_FETCH_BIO, new_callable=AsyncMock) as mock_bio,
        patch(PATCH_FIND_IMAGE, new_callable=AsyncMock),
        patch(PATCH_GEMINI_CLIENT) as mock_client_cls,
    ):
        mock_client_cls.return_value = MagicMock()

        from app.services.player_enrichment_service import run_enrichment_sweep

        result = await run_enrichment_sweep(session_factory)

    # The already-attempted stub should not be processed
    mock_bio.assert_not_called()
    assert result.players_attempted == 0


# ---------------------------------------------------------------------------
# 2. enrich_player — idempotency and selective field filling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enrich_player_fills_empty_fields_only(
    schema_session_factory: Any,
    db_session: AsyncSession,
) -> None:
    """enrich_player only updates fields that are currently None on the player.

    A field already set (e.g. school='Kentucky') should not be overwritten
    even though the Gemini response includes a different value.
    """
    player = await _stub_player(db_session, "Devon Mitchell")
    player.school = "Kentucky"  # pre-existing value — should not be clobbered
    await db_session.commit()
    player_id = player.id

    bio_data_different_school = {**_MOCK_BIO_DATA, "school": "Duke"}

    with (
        patch(PATCH_FETCH_BIO, new_callable=AsyncMock, return_value=bio_data_different_school),
        patch(PATCH_FIND_IMAGE, new_callable=AsyncMock, return_value=None),
        patch(PATCH_GENERATE_PORTRAIT, new_callable=AsyncMock),
        patch(PATCH_GEMINI_CLIENT) as mock_client_cls,
    ):
        mock_client_cls.return_value = MagicMock()

        from app.services.player_enrichment_service import enrich_player

        async with schema_session_factory() as db:
            read_player = await db.get(PlayerMaster, player_id)
            assert read_player is not None
            single = await enrich_player(db, read_player, _session_factory=schema_session_factory)

    assert single.error is None
    # The player's school should still be Kentucky (not overwritten)
    async with schema_session_factory() as verify_db:
        updated = await verify_db.get(PlayerMaster, player_id)
        assert updated is not None
        assert updated.school == "Kentucky"
        # But other empty fields should be filled
        assert updated.birth_city == "Atlanta"
        assert updated.enrichment_attempted_at is not None


@pytest.mark.asyncio
async def test_enrich_player_stamps_attempted_even_on_fetch_failure(
    schema_session_factory: Any,
    db_session: AsyncSession,
) -> None:
    """enrich_player stamps enrichment_attempted_at even when external fetches fail.

    When Gemini and Wikimedia both raise exceptions, the enrichment pipeline
    catches them (they are stored in _FetchedData), applies no changes
    (enriched=False), but still stamps enrichment_attempted_at so the sweep
    does not retry this player.

    Note: a fetch failure is NOT reported as SingleEnrichmentResult.error;
    the result is enriched=False, error=None.  The field is only set for
    hard internal/DB failures.
    """
    player = await _stub_player(db_session, "Marcus Webb")
    await db_session.commit()
    player_id = player.id

    with (
        patch(PATCH_FETCH_BIO, new_callable=AsyncMock, side_effect=RuntimeError("network error")),
        patch(PATCH_FIND_IMAGE, new_callable=AsyncMock, side_effect=RuntimeError("network error")),
        patch(PATCH_GEMINI_CLIENT) as mock_client_cls,
    ):
        mock_client_cls.return_value = MagicMock()

        from app.services.player_enrichment_service import enrich_player

        async with schema_session_factory() as db:
            read_player = await db.get(PlayerMaster, player_id)
            assert read_player is not None
            single = await enrich_player(db, read_player, _session_factory=schema_session_factory)

    # Fetch failure → enriched=False, error=None (fetch errors are logged, not raised)
    assert single.enriched is False
    # enrichment_attempted_at must still be stamped (prevents sweep from retrying)
    async with schema_session_factory() as verify_db:
        updated = await verify_db.get(PlayerMaster, player_id)
        assert updated is not None
        assert updated.enrichment_attempted_at is not None


# ---------------------------------------------------------------------------
# 3. enqueue_enrichment — creates job rows, dedup, cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_creates_job_rows(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    """enqueue_enrichment inserts one PlayerEnrichmentJob per eligible player."""
    player_a = await _stub_player(db_session, "Kai Thomas")
    player_b = await _stub_player(db_session, "Leo Barnes")
    await db_session.commit()

    async with session_factory() as db:
        async with db.begin():
            enqueued = await enqueue_enrichment(
                db,
                [player_a.id, player_b.id],  # type: ignore[list-item]
                source="admin_bulk",
                user_id=None,
            )

    assert sorted(enqueued) == sorted([player_a.id, player_b.id])  # type: ignore[type-var]

    # Verify DB rows
    async with session_factory() as verify_db:
        result = await verify_db.execute(
            select(PlayerEnrichmentJob)  # type: ignore[call-overload]
            .where(PlayerEnrichmentJob.player_id.in_([player_a.id, player_b.id]))  # type: ignore[attr-defined]
        )
        jobs = result.scalars().all()
        assert len(jobs) == 2
        assert all(j.state == "queued" for j in jobs)
        assert all(j.source == "admin_bulk" for j in jobs)


@pytest.mark.asyncio
async def test_enqueue_dedup_skips_in_flight(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    """enqueue_enrichment does not create a second job for a player already queued/running."""
    player = await _stub_player(db_session, "Omar Diallo")
    await db_session.commit()

    # First enqueue
    async with session_factory() as db:
        async with db.begin():
            first = await enqueue_enrichment(
                db, [player.id], source="admin_single", user_id=None  # type: ignore[list-item]
            )
    assert len(first) == 1

    # Second enqueue for the same player
    async with session_factory() as db:
        async with db.begin():
            second = await enqueue_enrichment(
                db, [player.id], source="admin_single", user_id=None  # type: ignore[list-item]
            )
    assert second == []

    # Only one job row should exist
    async with session_factory() as verify_db:
        result = await verify_db.execute(
            select(PlayerEnrichmentJob)  # type: ignore[call-overload]
            .where(PlayerEnrichmentJob.player_id == player.id)  # type: ignore[arg-type]
        )
        jobs = result.scalars().all()
        assert len(jobs) == 1


@pytest.mark.asyncio
async def test_enqueue_respects_per_request_cap(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    """enqueue_enrichment enforces MAX_ENQUEUE_PER_REQUEST."""
    # Create more players than the cap allows
    players = []
    for i in range(MAX_ENQUEUE_PER_REQUEST + 5):
        p = await _stub_player(db_session, f"Player Cap {i}")
        players.append(p)
    await db_session.commit()

    player_ids = [p.id for p in players]

    async with session_factory() as db:
        async with db.begin():
            enqueued = await enqueue_enrichment(
                db, player_ids, source="admin_bulk", user_id=None  # type: ignore[arg-type]
            )

    assert len(enqueued) <= MAX_ENQUEUE_PER_REQUEST


# ---------------------------------------------------------------------------
# 4. drain_enrichment_queue — state transitions and error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drain_transitions_queued_to_succeeded(
    schema_session_factory: Any,
    db_session: AsyncSession,
) -> None:
    """drain_enrichment_queue transitions a queued job to succeeded and stamps timestamps."""
    player = await _stub_player(db_session, "Finn Okafor")
    await db_session.commit()

    # Insert a queued job directly
    async with schema_session_factory() as db:
        async with db.begin():
            job = PlayerEnrichmentJob(
                player_id=player.id,
                state="queued",
                source="admin_single",
                created_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
            db.add(job)

    with (
        patch(PATCH_FETCH_BIO, new_callable=AsyncMock, return_value=_MOCK_BIO_DATA),
        patch(PATCH_FIND_IMAGE, new_callable=AsyncMock, return_value=None),
        patch(PATCH_GENERATE_PORTRAIT, new_callable=AsyncMock),
        patch(PATCH_GEMINI_CLIENT_QUEUE) as mock_client_cls,
    ):
        mock_client_cls.return_value = MagicMock()
        result = await drain_enrichment_queue(schema_session_factory, limit=10)

    assert result.claimed == 1
    assert result.succeeded == 1
    assert result.failed == 0

    # Verify the job row transitioned to succeeded
    async with schema_session_factory() as verify_db:
        stmt = select(PlayerEnrichmentJob)  # type: ignore[call-overload]
        res = await verify_db.execute(stmt)
        jobs = res.scalars().all()
        assert len(jobs) == 1
        assert jobs[0].state == "succeeded"
        assert jobs[0].completed_at is not None
        assert jobs[0].started_at is not None


@pytest.mark.asyncio
async def test_drain_transitions_to_failed_on_error(
    schema_session_factory: Any,
    db_session: AsyncSession,
) -> None:
    """drain_enrichment_queue sets state=failed and error_message on a hard write error.

    To produce a job-level failure (as opposed to a mere "nothing enriched"
    outcome), we patch _apply_enrichment to raise an exception during the
    DB write phase.  This causes _enrich_player_with_factory to catch the
    error, set error_message, and return SingleEnrichmentResult(error=...).
    The drain then marks the job failed and stamps completed_at.
    """
    player = await _stub_player(db_session, "Darius Flynn")
    await db_session.commit()

    async with schema_session_factory() as db:
        async with db.begin():
            job = PlayerEnrichmentJob(
                player_id=player.id,
                state="queued",
                source="admin_single",
                created_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
            db.add(job)

    with (
        patch(PATCH_FETCH_BIO, new_callable=AsyncMock, return_value=_MOCK_BIO_DATA),
        patch(PATCH_FIND_IMAGE, new_callable=AsyncMock, return_value=None),
        patch(
            "app.services.player_enrichment_service._apply_enrichment",
            new_callable=AsyncMock,
            side_effect=RuntimeError("DB write error"),
        ),
        patch(PATCH_GEMINI_CLIENT_QUEUE) as mock_client_cls,
    ):
        mock_client_cls.return_value = MagicMock()
        result = await drain_enrichment_queue(schema_session_factory, limit=10)

    assert result.failed == 1
    assert result.succeeded == 0

    async with schema_session_factory() as verify_db:
        stmt = select(PlayerEnrichmentJob)  # type: ignore[call-overload]
        res = await verify_db.execute(stmt)
        jobs = res.scalars().all()
        assert jobs[0].state == "failed"
        assert jobs[0].error_message is not None
        assert jobs[0].completed_at is not None

    # enrichment_attempted_at should also be stamped via the failure-stamp path
    async with schema_session_factory() as verify_db:
        updated = await verify_db.get(PlayerMaster, player.id)
        assert updated is not None
        assert updated.enrichment_attempted_at is not None


@pytest.mark.asyncio
async def test_drain_reclaims_stale_running_jobs(
    schema_session_factory: Any,
    db_session: AsyncSession,
) -> None:
    """drain_enrichment_queue resets stale 'running' jobs to 'queued'.

    A job stuck in 'running' for longer than _STALE_RUNNING_SECONDS
    is reclaimed (transitioned back to 'queued') so it can be re-processed.
    """
    from app.services.enrichment_queue_service import _STALE_RUNNING_SECONDS

    player = await _stub_player(db_session, "Chris Osei")
    await db_session.commit()

    stale_started_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
        seconds=_STALE_RUNNING_SECONDS + 60
    )

    async with schema_session_factory() as db:
        async with db.begin():
            job = PlayerEnrichmentJob(
                player_id=player.id,
                state="running",  # stuck in running
                source="admin_single",
                started_at=stale_started_at,
                created_at=stale_started_at,
            )
            db.add(job)

    with (
        patch(PATCH_FETCH_BIO, new_callable=AsyncMock, return_value=_MOCK_BIO_DATA),
        patch(PATCH_FIND_IMAGE, new_callable=AsyncMock, return_value=None),
        patch(PATCH_GENERATE_PORTRAIT, new_callable=AsyncMock),
        patch(PATCH_GEMINI_CLIENT_QUEUE) as mock_client_cls,
    ):
        mock_client_cls.return_value = MagicMock()
        result = await drain_enrichment_queue(schema_session_factory, limit=10)

    # Should have reclaimed the stale job and then processed it
    assert result.reclaimed_stale == 1
    assert result.claimed == 1
    assert result.succeeded == 1


# ---------------------------------------------------------------------------
# 5. enrichment_status — reflects job state in DB
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enrichment_status_reflects_job_state(
    schema_session_factory: Any,
    db_session: AsyncSession,
) -> None:
    """enrichment_status returns the most-recent job state per player from the DB."""
    player = await _stub_player(db_session, "Nathan Cross")
    await db_session.commit()

    async with schema_session_factory() as db:
        async with db.begin():
            job = PlayerEnrichmentJob(
                player_id=player.id,
                state="queued",
                source="admin_single",
                created_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
            db.add(job)

    async with schema_session_factory() as db:
        status = await enrichment_status(db, [player.id])  # type: ignore[list-item]

    assert player.id in status
    assert status[player.id].state == "queued"
    assert status[player.id].error is None


@pytest.mark.asyncio
async def test_enrichment_status_omits_players_without_jobs(
    schema_session_factory: Any,
    db_session: AsyncSession,
) -> None:
    """enrichment_status omits players that have no job records."""
    player = await _stub_player(db_session, "Eli Santos")
    await db_session.commit()

    async with schema_session_factory() as db:
        status = await enrichment_status(db, [player.id])  # type: ignore[list-item]

    assert status == {}


# ---------------------------------------------------------------------------
# 6. Cron backstop wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cron_runner_drain_is_called() -> None:
    """cron_runner._run_drain_queue_job calls drain_enrichment_queue with SessionLocal."""
    with patch(
        "app.cli.cron_runner.drain_enrichment_queue", new_callable=AsyncMock
    ) as mock_drain:
        mock_drain.return_value = DrainResult()

        from app.cli.cron_runner import _run_drain_queue_job

        await _run_drain_queue_job()

        mock_drain.assert_awaited_once()
        # Verify it was called with the SessionLocal factory
        args, kwargs = mock_drain.call_args
        from app.utils.db_async import SessionLocal

        assert args[0] is SessionLocal
