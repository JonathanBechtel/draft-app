"""Integration tests for the PlayerEnrichmentJob schema.

Covers:
- Insert / select round-trip.
- Foreign key to players_master (player_id).
- Optional FK to auth_users (requested_by_user_id).
- All valid state strings are storable.
- Composite index (state, created_at) and player_id index are present in the DB.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.player_enrichment_jobs import PlayerEnrichmentJob
from app.schemas.players_master import PlayerMaster


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _make_player(suffix: str) -> PlayerMaster:
    return PlayerMaster(
        display_name=f"Test Player {suffix}",
        first_name="Test",
        last_name=f"Player{suffix}",
        draft_year=2025,
        is_stub=True,
    )


def _make_job(player_id: int, *, state: str = "queued", source: str = "admin_single") -> PlayerEnrichmentJob:
    return PlayerEnrichmentJob(
        player_id=player_id,
        state=state,
        source=source,
        created_at=_now(),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_insert_and_select_round_trip(db_session: AsyncSession) -> None:
    """A PlayerEnrichmentJob inserts and round-trips cleanly via SELECT."""
    player = _make_player("A")
    db_session.add(player)
    await db_session.flush()

    job = _make_job(player.id)  # type: ignore[arg-type]
    db_session.add(job)
    await db_session.flush()

    assert job.id is not None
    assert job.player_id == player.id
    assert job.state == "queued"
    assert job.source == "admin_single"
    assert job.started_at is None
    assert job.completed_at is None
    assert job.error_message is None
    assert job.requested_by_user_id is None

    # Verify via raw SELECT
    result = await db_session.execute(
        text("SELECT state, source, player_id FROM player_enrichment_jobs WHERE id = :id"),
        {"id": job.id},
    )
    row = result.one()
    assert row.state == "queued"
    assert row.source == "admin_single"
    assert row.player_id == player.id


@pytest.mark.asyncio
async def test_foreign_key_to_players_master(db_session: AsyncSession) -> None:
    """player_id FK to players_master is enforced — nonexistent player_id fails."""
    bad_job = PlayerEnrichmentJob(
        player_id=999_999,
        state="queued",
        source="admin_single",
        created_at=_now(),
    )
    db_session.add(bad_job)
    with pytest.raises(Exception):
        await db_session.flush()


@pytest.mark.asyncio
async def test_all_state_values_storable(db_session: AsyncSession) -> None:
    """Each valid state string round-trips through Postgres without error."""
    player = _make_player("B")
    db_session.add(player)
    await db_session.flush()

    for state in ("queued", "running", "succeeded", "failed"):
        job = _make_job(player.id, state=state)  # type: ignore[arg-type]
        db_session.add(job)
    await db_session.flush()

    result = await db_session.execute(
        text("SELECT state FROM player_enrichment_jobs WHERE player_id = :pid ORDER BY id"),
        {"pid": player.id},
    )
    stored_states = [row.state for row in result.fetchall()]
    assert stored_states == ["queued", "running", "succeeded", "failed"]


@pytest.mark.asyncio
async def test_source_values_storable(db_session: AsyncSession) -> None:
    """Each valid source string round-trips cleanly."""
    player = _make_player("C")
    db_session.add(player)
    await db_session.flush()

    for source in ("admin_single", "admin_bulk", "cron"):
        job = _make_job(player.id, source=source)  # type: ignore[arg-type]
        db_session.add(job)
    await db_session.flush()

    result = await db_session.execute(
        text("SELECT source FROM player_enrichment_jobs WHERE player_id = :pid ORDER BY id"),
        {"pid": player.id},
    )
    stored_sources = [row.source for row in result.fetchall()]
    assert stored_sources == ["admin_single", "admin_bulk", "cron"]


@pytest.mark.asyncio
async def test_timestamps_and_error_message(db_session: AsyncSession) -> None:
    """started_at, completed_at, and error_message are storable and nullable."""
    player = _make_player("D")
    db_session.add(player)
    await db_session.flush()

    now = _now()
    job = PlayerEnrichmentJob(
        player_id=player.id,  # type: ignore[arg-type]
        state="failed",
        source="cron",
        created_at=now,
        started_at=now,
        completed_at=now,
        error_message="Gemini quota exceeded",
    )
    db_session.add(job)
    await db_session.flush()

    result = await db_session.execute(
        text(
            "SELECT started_at, completed_at, error_message"
            " FROM player_enrichment_jobs WHERE id = :id"
        ),
        {"id": job.id},
    )
    row = result.one()
    assert row.started_at is not None
    assert row.completed_at is not None
    assert row.error_message == "Gemini quota exceeded"


@pytest.mark.asyncio
async def test_indexes_present(db_session: AsyncSession, test_schema: str) -> None:
    """The composite (state, created_at) index and player_id index both exist."""
    result = await db_session.execute(
        text(
            "SELECT indexname FROM pg_indexes"
            " WHERE schemaname = :schema AND tablename = 'player_enrichment_jobs'"
        ),
        {"schema": test_schema},
    )
    index_names = {row.indexname for row in result.fetchall()}

    assert "ix_enrichment_jobs_state_created" in index_names, (
        f"Missing ix_enrichment_jobs_state_created; found: {index_names}"
    )
    assert "ix_enrichment_jobs_player" in index_names, (
        f"Missing ix_enrichment_jobs_player; found: {index_names}"
    )


@pytest.mark.asyncio
async def test_is_stub_index_present_on_players_master(
    db_session: AsyncSession, test_schema: str
) -> None:
    """The ix_players_master_is_stub_created index exists on players_master."""
    result = await db_session.execute(
        text(
            "SELECT indexname FROM pg_indexes"
            " WHERE schemaname = :schema AND tablename = 'players_master'"
        ),
        {"schema": test_schema},
    )
    index_names = {row.indexname for row in result.fetchall()}

    assert "ix_players_master_is_stub_created" in index_names, (
        f"Missing ix_players_master_is_stub_created; found: {index_names}"
    )


@pytest.mark.asyncio
async def test_multiple_jobs_for_same_player(db_session: AsyncSession) -> None:
    """Multiple enrichment jobs for the same player are all stored (no unique constraint)."""
    player = _make_player("E")
    db_session.add(player)
    await db_session.flush()

    job1 = _make_job(player.id, state="succeeded")  # type: ignore[arg-type]
    job2 = _make_job(player.id, state="queued")  # type: ignore[arg-type]
    db_session.add(job1)
    db_session.add(job2)
    await db_session.flush()

    result = await db_session.execute(
        text(
            "SELECT COUNT(*) AS cnt FROM player_enrichment_jobs WHERE player_id = :pid"
        ),
        {"pid": player.id},
    )
    assert result.scalar_one() == 2
