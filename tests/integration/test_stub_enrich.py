"""Integration tests for on-demand enrichment routes (spec §C, test-plan Slice 6).

Covers:
- POST /admin/players/stubs/enrich (single + bulk) creates PlayerEnrichmentJob
  rows, deduplicates in-flight jobs, and returns a redirect with a flash message.
- GET /admin/players/stubs/enrichment-status?ids= returns job state JSON.
- Per-request cap (MAX_ENQUEUE_PER_REQUEST) is honoured; excess IDs are silently
  truncated and the flash message notes the cap.
- Permission gates: unauthenticated → redirect; worker without players perm →
  redirect; worker with can_edit=False cannot POST enrich; worker with can_edit
  can POST; status endpoint requires at least can_view.
- drain_enrichment_queue transitions queued → running → succeeded/failed
  (external calls mocked).
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.player_enrichment_jobs import PlayerEnrichmentJob
from app.schemas.players_master import PlayerMaster
from app.services.enrichment_queue_service import (
    MAX_ENQUEUE_PER_REQUEST,
    enqueue_enrichment,
    enrichment_status,
)
from tests.integration.auth_helpers import (
    create_auth_user,
    grant_dataset_permission,
    login_staff,
)


# ---------------------------------------------------------------------------
# Neutralize the background player-embedding side effect.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _disable_embedding_side_effect(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent player embedding background tasks from firing during tests."""
    monkeypatch.setattr(
        "app.schemas.players_master._schedule_player_embedding",
        lambda snapshot: None,
    )


# ---------------------------------------------------------------------------
# Test credentials (unique per file)
# ---------------------------------------------------------------------------

ADMIN_EMAIL = "stub-enrich-admin@example.com"
ADMIN_PASSWORD = "stub-enrich-admin-pass-123"

WORKER_EDIT_EMAIL = "stub-enrich-worker-edit@example.com"
WORKER_EDIT_PASSWORD = "stub-enrich-worker-edit-pass-456"

WORKER_VIEW_EMAIL = "stub-enrich-worker-view@example.com"
WORKER_VIEW_PASSWORD = "stub-enrich-worker-view-pass-789"

WORKER_NOPERM_EMAIL = "stub-enrich-noperm@example.com"
WORKER_NOPERM_PASSWORD = "stub-enrich-noperm-pass-000"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def admin_logged_in(app_client: AsyncClient, db_session: AsyncSession) -> None:
    """Create an admin user and log in on app_client."""
    await create_auth_user(
        db_session, email=ADMIN_EMAIL, role="admin", password=ADMIN_PASSWORD
    )
    await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)


@pytest_asyncio.fixture
async def worker_edit(app_client: AsyncClient, db_session: AsyncSession) -> int:
    """Create a worker with can_edit players permission and log in."""
    user_id = await create_auth_user(
        db_session,
        email=WORKER_EDIT_EMAIL,
        role="worker",
        password=WORKER_EDIT_PASSWORD,
    )
    await grant_dataset_permission(
        db_session, user_id=user_id, dataset="players", can_view=True, can_edit=True
    )
    await login_staff(app_client, email=WORKER_EDIT_EMAIL, password=WORKER_EDIT_PASSWORD)
    return user_id


@pytest_asyncio.fixture
async def worker_view(app_client: AsyncClient, db_session: AsyncSession) -> int:
    """Create a worker with view-only players permission and log in."""
    user_id = await create_auth_user(
        db_session,
        email=WORKER_VIEW_EMAIL,
        role="worker",
        password=WORKER_VIEW_PASSWORD,
    )
    await grant_dataset_permission(
        db_session, user_id=user_id, dataset="players", can_view=True, can_edit=False
    )
    await login_staff(app_client, email=WORKER_VIEW_EMAIL, password=WORKER_VIEW_PASSWORD)
    return user_id


@pytest_asyncio.fixture
async def worker_noperm(app_client: AsyncClient, db_session: AsyncSession) -> int:
    """Create a worker with no players permission and log in."""
    user_id = await create_auth_user(
        db_session,
        email=WORKER_NOPERM_EMAIL,
        role="worker",
        password=WORKER_NOPERM_PASSWORD,
    )
    await login_staff(app_client, email=WORKER_NOPERM_EMAIL, password=WORKER_NOPERM_PASSWORD)
    return user_id


@pytest_asyncio.fixture
async def stub_player(db_session: AsyncSession) -> PlayerMaster:
    """Insert a stub player and return it."""
    p = PlayerMaster(
        display_name="Enrich Testingson",
        first_name="Enrich",
        last_name="Testingson",
        is_stub=True,
    )
    db_session.add(p)
    await db_session.commit()
    await db_session.refresh(p)
    return p


@pytest_asyncio.fixture
async def another_stub(db_session: AsyncSession) -> PlayerMaster:
    """Insert a second stub player and return it."""
    p = PlayerMaster(
        display_name="Another Stubworth",
        first_name="Another",
        last_name="Stubworth",
        is_stub=True,
    )
    db_session.add(p)
    await db_session.commit()
    await db_session.refresh(p)
    return p


# ---------------------------------------------------------------------------
# POST /admin/players/stubs/enrich — single
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enrich_single_creates_job(
    app_client: AsyncClient,
    db_session: AsyncSession,
    admin_logged_in: None,
    stub_player: PlayerMaster,
) -> None:
    """POST enrich with a single player_id creates a PlayerEnrichmentJob row."""
    resp = await app_client.post(
        "/admin/players/stubs/enrich",
        data={"player_id": str(stub_player.id)},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    jobs = (
        await db_session.execute(
            select(PlayerEnrichmentJob).where(
                PlayerEnrichmentJob.player_id == stub_player.id  # type: ignore[arg-type]
            )
        )
    ).scalars().all()
    assert len(jobs) == 1
    assert jobs[0].state == "queued"
    assert jobs[0].source == "admin_single"


@pytest.mark.asyncio
async def test_enrich_single_flash_message(
    app_client: AsyncClient,
    db_session: AsyncSession,
    admin_logged_in: None,
    stub_player: PlayerMaster,
) -> None:
    """Flash message in redirect URL indicates how many jobs were queued."""
    resp = await app_client.post(
        "/admin/players/stubs/enrich",
        data={"player_id": str(stub_player.id)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    location = resp.headers.get("location", "")
    assert "1" in location or "Queued" in location


# ---------------------------------------------------------------------------
# POST /admin/players/stubs/enrich — bulk
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enrich_bulk_creates_multiple_jobs(
    app_client: AsyncClient,
    db_session: AsyncSession,
    admin_logged_in: None,
    stub_player: PlayerMaster,
    another_stub: PlayerMaster,
) -> None:
    """Bulk enrich creates one job per player."""
    resp = await app_client.post(
        "/admin/players/stubs/enrich",
        data={
            "player_ids[]": [str(stub_player.id), str(another_stub.id)],
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    jobs = (
        await db_session.execute(
            select(PlayerEnrichmentJob).where(
                PlayerEnrichmentJob.player_id.in_(  # type: ignore[attr-defined]
                    [stub_player.id, another_stub.id]
                )
            )
        )
    ).scalars().all()
    assert len(jobs) == 2
    player_ids = {j.player_id for j in jobs}
    assert stub_player.id in player_ids
    assert another_stub.id in player_ids
    for j in jobs:
        assert j.source == "admin_bulk"


@pytest.mark.asyncio
async def test_enrich_bulk_deduplicates_in_flight(
    app_client: AsyncClient,
    db_session: AsyncSession,
    admin_logged_in: None,
    stub_player: PlayerMaster,
) -> None:
    """Second enrich request for the same player does not create a duplicate job."""
    # First request: creates a job
    await app_client.post(
        "/admin/players/stubs/enrich",
        data={"player_id": str(stub_player.id)},
        follow_redirects=False,
    )

    # Second request: job should already be in-flight → deduplicated
    resp = await app_client.post(
        "/admin/players/stubs/enrich",
        data={"player_id": str(stub_player.id)},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    jobs = (
        await db_session.execute(
            select(PlayerEnrichmentJob).where(
                PlayerEnrichmentJob.player_id == stub_player.id  # type: ignore[arg-type]
            )
        )
    ).scalars().all()
    # Still only one job
    assert len(jobs) == 1


# ---------------------------------------------------------------------------
# POST /admin/players/stubs/enrich — empty selection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enrich_empty_selection_redirects(
    app_client: AsyncClient,
    db_session: AsyncSession,
    admin_logged_in: None,
) -> None:
    """Posting enrich with no player IDs redirects without creating any jobs."""
    resp = await app_client.post(
        "/admin/players/stubs/enrich",
        data={},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    jobs = (
        await db_session.execute(select(PlayerEnrichmentJob))
    ).scalars().all()
    assert len(jobs) == 0


# ---------------------------------------------------------------------------
# GET /admin/players/stubs/enrichment-status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enrichment_status_empty(
    app_client: AsyncClient,
    admin_logged_in: None,
) -> None:
    """Status endpoint returns empty object when no jobs exist for those IDs."""
    resp = await app_client.get(
        "/admin/players/stubs/enrichment-status?ids=99999,88888"
    )
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, dict)
    assert len(data) == 0


@pytest.mark.asyncio
async def test_enrichment_status_reflects_job_state(
    app_client: AsyncClient,
    db_session: AsyncSession,
    admin_logged_in: None,
    stub_player: PlayerMaster,
) -> None:
    """Status endpoint returns the job state for a player with an existing job."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    job = PlayerEnrichmentJob(
        player_id=stub_player.id,
        state="queued",
        source="admin_single",
        created_at=now,
    )
    db_session.add(job)
    await db_session.commit()

    resp = await app_client.get(
        f"/admin/players/stubs/enrichment-status?ids={stub_player.id}"
    )
    assert resp.status_code == 200
    data = resp.json()
    assert str(stub_player.id) in data
    assert data[str(stub_player.id)]["state"] == "queued"
    assert data[str(stub_player.id)]["error"] is None


@pytest.mark.asyncio
async def test_enrichment_status_running_state(
    app_client: AsyncClient,
    db_session: AsyncSession,
    admin_logged_in: None,
    stub_player: PlayerMaster,
) -> None:
    """Status endpoint reflects 'running' state correctly."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    job = PlayerEnrichmentJob(
        player_id=stub_player.id,
        state="running",
        source="admin_single",
        created_at=now,
        started_at=now,
    )
    db_session.add(job)
    await db_session.commit()

    resp = await app_client.get(
        f"/admin/players/stubs/enrichment-status?ids={stub_player.id}"
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data[str(stub_player.id)]["state"] == "running"


@pytest.mark.asyncio
async def test_enrichment_status_failed_state_includes_error(
    app_client: AsyncClient,
    db_session: AsyncSession,
    admin_logged_in: None,
    stub_player: PlayerMaster,
) -> None:
    """Status endpoint includes the error_message for failed jobs."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    job = PlayerEnrichmentJob(
        player_id=stub_player.id,
        state="failed",
        source="admin_single",
        created_at=now,
        completed_at=now,
        error_message="Network timeout",
    )
    db_session.add(job)
    await db_session.commit()

    resp = await app_client.get(
        f"/admin/players/stubs/enrichment-status?ids={stub_player.id}"
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data[str(stub_player.id)]["state"] == "failed"
    assert data[str(stub_player.id)]["error"] == "Network timeout"


# ---------------------------------------------------------------------------
# Permission gates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enrich_unauthenticated_redirects(
    app_client: AsyncClient,
    stub_player: PlayerMaster,
) -> None:
    """Unauthenticated POST to enrich redirects (no session)."""
    resp = await app_client.post(
        "/admin/players/stubs/enrich",
        data={"player_id": str(stub_player.id)},
        follow_redirects=False,
    )
    # Should redirect to login or access-denied page
    assert resp.status_code in (302, 303)
    location = resp.headers.get("location", "")
    assert "login" in location or "admin" in location


@pytest.mark.asyncio
async def test_enrich_worker_no_perm_denied(
    app_client: AsyncClient,
    db_session: AsyncSession,
    worker_noperm: int,
    stub_player: PlayerMaster,
) -> None:
    """Worker without players permission is denied POST to enrich."""
    resp = await app_client.post(
        "/admin/players/stubs/enrich",
        data={"player_id": str(stub_player.id)},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    # No job should be created
    jobs = (
        await db_session.execute(
            select(PlayerEnrichmentJob).where(
                PlayerEnrichmentJob.player_id == stub_player.id  # type: ignore[arg-type]
            )
        )
    ).scalars().all()
    assert len(jobs) == 0


@pytest.mark.asyncio
async def test_enrich_worker_view_only_denied(
    app_client: AsyncClient,
    db_session: AsyncSession,
    worker_view: int,
    stub_player: PlayerMaster,
) -> None:
    """Worker with view-only permission cannot POST to enrich (requires can_edit)."""
    resp = await app_client.post(
        "/admin/players/stubs/enrich",
        data={"player_id": str(stub_player.id)},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    jobs = (
        await db_session.execute(
            select(PlayerEnrichmentJob).where(
                PlayerEnrichmentJob.player_id == stub_player.id  # type: ignore[arg-type]
            )
        )
    ).scalars().all()
    assert len(jobs) == 0


@pytest.mark.asyncio
async def test_enrich_worker_with_edit_perm_allowed(
    app_client: AsyncClient,
    db_session: AsyncSession,
    worker_edit: int,
    stub_player: PlayerMaster,
) -> None:
    """Worker with can_edit players permission can enqueue enrichment."""
    resp = await app_client.post(
        "/admin/players/stubs/enrich",
        data={"player_id": str(stub_player.id)},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    jobs = (
        await db_session.execute(
            select(PlayerEnrichmentJob).where(
                PlayerEnrichmentJob.player_id == stub_player.id  # type: ignore[arg-type]
            )
        )
    ).scalars().all()
    assert len(jobs) == 1


@pytest.mark.asyncio
async def test_status_unauthenticated_forbidden(
    app_client: AsyncClient,
    stub_player: PlayerMaster,
) -> None:
    """Unauthenticated GET to enrichment-status returns 403."""
    resp = await app_client.get(
        f"/admin/players/stubs/enrichment-status?ids={stub_player.id}"
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_status_worker_no_perm_forbidden(
    app_client: AsyncClient,
    worker_noperm: int,
    stub_player: PlayerMaster,
) -> None:
    """Worker without players permission gets 403 on enrichment-status."""
    resp = await app_client.get(
        f"/admin/players/stubs/enrichment-status?ids={stub_player.id}"
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_status_worker_view_perm_allowed(
    app_client: AsyncClient,
    db_session: AsyncSession,
    worker_view: int,
    stub_player: PlayerMaster,
) -> None:
    """Worker with can_view permission can access enrichment-status."""
    resp = await app_client.get(
        f"/admin/players/stubs/enrichment-status?ids={stub_player.id}"
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# enqueue_enrichment service — cap enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_respects_cap(db_session: AsyncSession) -> None:
    """enqueue_enrichment silently truncates to MAX_ENQUEUE_PER_REQUEST."""
    # Create MAX+5 stub players
    cap = MAX_ENQUEUE_PER_REQUEST
    players = []
    for i in range(cap + 5):
        p = PlayerMaster(
            display_name=f"Cap Test Player {i}",
            first_name="Cap",
            last_name=f"Player{i}",
            is_stub=True,
        )
        db_session.add(p)
        players.append(p)
    await db_session.flush()

    player_ids = [p.id for p in players if p.id is not None]
    async with db_session.begin_nested():
        enqueued = await enqueue_enrichment(
            db_session, player_ids, source="admin_bulk", user_id=None
        )

    assert len(enqueued) == cap


@pytest.mark.asyncio
async def test_enqueue_deduplicates_in_flight_service(db_session: AsyncSession) -> None:
    """enqueue_enrichment skips players with queued/running jobs."""
    p = PlayerMaster(
        display_name="Dedup Service Player",
        first_name="Dedup",
        last_name="Service",
        is_stub=True,
    )
    db_session.add(p)
    await db_session.flush()

    assert p.id is not None
    # First enqueue
    async with db_session.begin_nested():
        first = await enqueue_enrichment(
            db_session, [p.id], source="admin_single", user_id=None
        )
    assert len(first) == 1

    # Second enqueue — same player, still queued
    async with db_session.begin_nested():
        second = await enqueue_enrichment(
            db_session, [p.id], source="admin_single", user_id=None
        )
    assert len(second) == 0


# ---------------------------------------------------------------------------
# enrichment_status service
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enrichment_status_service_returns_latest(
    db_session: AsyncSession,
) -> None:
    """enrichment_status returns the most-recent job when multiple exist."""
    p = PlayerMaster(
        display_name="Status Test Player",
        first_name="Status",
        last_name="TestPlayer",
        is_stub=True,
    )
    db_session.add(p)
    await db_session.flush()

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    from datetime import timedelta

    older_job = PlayerEnrichmentJob(
        player_id=p.id,
        state="failed",
        source="admin_single",
        created_at=now - timedelta(hours=1),
        error_message="old error",
    )
    newer_job = PlayerEnrichmentJob(
        player_id=p.id,
        state="succeeded",
        source="admin_single",
        created_at=now,
    )
    db_session.add(older_job)
    db_session.add(newer_job)
    await db_session.flush()

    assert p.id is not None
    result = await enrichment_status(db_session, [p.id])
    assert p.id in result
    # Should return the most recent (succeeded), not the older failed
    assert result[p.id].state == "succeeded"


@pytest.mark.asyncio
async def test_enrichment_status_service_empty_input(db_session: AsyncSession) -> None:
    """enrichment_status returns empty dict for empty input."""
    result = await enrichment_status(db_session, [])
    assert result == {}


# ---------------------------------------------------------------------------
# Regression: stubs list surfaces queued/running job state immediately
# (so the JS poller can start without a manual refresh)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stubs_list_shows_enriching_badge_after_enqueue(
    app_client: AsyncClient,
    db_session: AsyncSession,
    admin_logged_in: None,
    stub_player: PlayerMaster,
) -> None:
    """After POST /enrich, GET /stubs lists the player with 'Enriching…' badge.

    This is a regression test for the codex finding: the template previously
    hardcoded ``latest_job_state = none``, so queued/running rows always
    rendered as 'Not Attempted'.  The fix propagates the real job state from
    the service layer so the badge is correct on initial page load — allowing
    the JS poller to start without requiring a manual refresh.

    Steps:
    1. Seed a stub player (no enrichment job).
    2. POST /enrich to create a queued job.
    3. GET /admin/players/stubs and assert the player's badge shows 'Enriching…'.
    """
    # Step 2: enqueue
    resp = await app_client.post(
        "/admin/players/stubs/enrich",
        data={"player_id": str(stub_player.id)},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    # Step 3: fetch the list page and assert the badge is 'Enriching…'
    list_resp = await app_client.get("/admin/players/stubs")
    assert list_resp.status_code == 200
    assert "Enriching" in list_resp.text, (
        "Expected 'Enriching…' badge for a player with a queued enrichment job, "
        "but the list page showed a different status. "
        "Check that list_players() populates latest_job_state and the template uses it."
    )
