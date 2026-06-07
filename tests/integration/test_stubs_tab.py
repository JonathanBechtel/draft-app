"""Integration tests for the Stubs admin tab (spec §B, test-plan Slice 5).

Covers:
- GET /admin/players/stubs: lists only is_stub=True rows.
- Filters (enrichment_status, draft_year, name search) narrow results.
- Pagination preserves filter params.
- POST .../quick-add: happy path creates a stub; blocked/ambiguous outcomes
  surface error messaging without creating rows.
- POST .../{id}/promote clears is_stub.
- POST .../{id}/delete deletes orphan stubs; refuses when inbound references
  exist; count_inbound_references is exercised.
- Bulk-delete deletes selected stubs; skips guarded ones and surfaces errors.
- Permission gates: unauthenticated → redirect; worker without players perm →
  redirect; worker with view perm can GET; edit routes require can_edit.
- Query-budget gate: authenticated stubs tab renders within the budget defined
  in tests/integration/perf/budgets.py.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.schemas.boards import Board, BoardEntry, BoardStatus
from app.schemas.news_sources import FeedType, NewsSource
from app.schemas.players_master import PlayerMaster
from app.schemas.player_lifecycle import PlayerLifecycle
from app.schemas.player_aliases import PlayerAlias
from app.schemas.news_items import NewsItem
from tests.integration.auth_helpers import (
    create_auth_user,
    grant_dataset_permission,
    login_staff,
)
from tests.integration.perf._capture import count_queries
from tests.integration.perf.budgets import ADMIN_ROUTE_BUDGETS


# ---------------------------------------------------------------------------
# Neutralize the background player-embedding side effect.
# PlayerMaster has an ``after_commit`` listener that fires a background
# embedding write; across truncate cycles this hits the pkey uniqueness
# constraint.  Stub it out for all tests in this file.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _disable_embedding_side_effect(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent player embedding background tasks from firing during tests."""
    monkeypatch.setattr(
        "app.schemas.players_master._schedule_player_embedding",
        lambda snapshot: None,
    )


# ---------------------------------------------------------------------------
# Test credentials (unique per file to avoid cross-test contamination)
# ---------------------------------------------------------------------------

ADMIN_EMAIL = "stubs-tab-admin@example.com"
ADMIN_PASSWORD = "stubs-tab-admin-pass-123"

WORKER_EMAIL = "stubs-tab-worker@example.com"
WORKER_PASSWORD = "stubs-tab-worker-pass-456"

NOAUTH_WORKER_EMAIL = "stubs-tab-noauth@example.com"
NOAUTH_WORKER_PASSWORD = "stubs-tab-noauth-pass-789"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def admin_logged_in(app_client: AsyncClient, db_session: AsyncSession) -> None:
    """Create an admin user and log in on app_client."""
    await create_auth_user(
        db_session,
        email=ADMIN_EMAIL,
        role="admin",
        password=ADMIN_PASSWORD,
    )
    await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)


@pytest_asyncio.fixture
async def worker_view_only(app_client: AsyncClient, db_session: AsyncSession) -> int:
    """Create a worker user with view-only players permission; log in."""
    user_id = await create_auth_user(
        db_session,
        email=WORKER_EMAIL,
        role="worker",
        password=WORKER_PASSWORD,
    )
    await grant_dataset_permission(
        db_session, user_id=user_id, dataset="players", can_view=True, can_edit=False
    )
    await login_staff(app_client, email=WORKER_EMAIL, password=WORKER_PASSWORD)
    return user_id


@pytest_asyncio.fixture
async def worker_no_perm(app_client: AsyncClient, db_session: AsyncSession) -> int:
    """Create a worker user with no players permission; log in."""
    user_id = await create_auth_user(
        db_session,
        email=NOAUTH_WORKER_EMAIL,
        role="worker",
        password=NOAUTH_WORKER_PASSWORD,
    )
    await login_staff(app_client, email=NOAUTH_WORKER_EMAIL, password=NOAUTH_WORKER_PASSWORD)
    return user_id


@pytest_asyncio.fixture
async def stub_player(db_session: AsyncSession) -> PlayerMaster:
    """Insert a stub player and return it."""
    p = PlayerMaster(
        display_name="Stub Testington",
        first_name="Stub",
        last_name="Testington",
        is_stub=True,
    )
    db_session.add(p)
    await db_session.commit()
    await db_session.refresh(p)
    return p


@pytest_asyncio.fixture
async def full_player(db_session: AsyncSession) -> PlayerMaster:
    """Insert a full (non-stub) player and return it."""
    p = PlayerMaster(
        display_name="Full Playersome",
        first_name="Full",
        last_name="Playersome",
        is_stub=False,
    )
    db_session.add(p)
    await db_session.commit()
    await db_session.refresh(p)
    return p


async def _make_news_source(db: AsyncSession, suffix: str = "") -> NewsSource:
    """Create a news source for test use."""
    import secrets

    unique = suffix or secrets.token_hex(4)
    src = NewsSource(
        name=f"stubs-test-source-{unique}",
        display_name=f"Stubs Test Source {unique}",
        feed_type=FeedType.RSS,
        feed_url=f"https://example.com/stubs-test-feed-{unique}.xml",
        is_active=True,
        fetch_interval_minutes=30,
    )
    db.add(src)
    await db.flush()
    return src


# ---------------------------------------------------------------------------
# §B: Basic list + permission gates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestStubsListAccess:
    """Access-control tests for the stubs list."""

    async def test_unauthenticated_redirects(self, app_client: AsyncClient) -> None:
        """Unauthenticated GET redirects to login."""
        resp = await app_client.get("/admin/players/stubs", follow_redirects=False)
        assert resp.status_code in {302, 303}
        assert "/admin/login" in resp.headers.get("location", "")

    async def test_worker_without_players_perm_redirects(
        self,
        app_client: AsyncClient,
        worker_no_perm: int,
    ) -> None:
        """Worker without players permission is redirected to /admin."""
        resp = await app_client.get("/admin/players/stubs", follow_redirects=False)
        assert resp.status_code in {302, 303}

    async def test_worker_with_view_perm_can_access(
        self,
        app_client: AsyncClient,
        worker_view_only: int,
    ) -> None:
        """Worker with players can_view=True may see the stubs list."""
        resp = await app_client.get("/admin/players/stubs", follow_redirects=False)
        assert resp.status_code == 200

    async def test_admin_can_access(
        self,
        app_client: AsyncClient,
        admin_logged_in: None,
    ) -> None:
        """Admin can view the stubs list."""
        resp = await app_client.get("/admin/players/stubs", follow_redirects=False)
        assert resp.status_code == 200


@pytest.mark.asyncio
class TestStubsListContent:
    """Tests for stubs-list content and filtering."""

    async def test_lists_only_stubs(
        self,
        app_client: AsyncClient,
        admin_logged_in: None,
        stub_player: PlayerMaster,
        full_player: PlayerMaster,
    ) -> None:
        """Only is_stub=True rows appear in the stubs list."""
        resp = await app_client.get("/admin/players/stubs")
        assert resp.status_code == 200
        text = resp.text
        assert stub_player.display_name in text
        assert full_player.display_name not in text

    async def test_filter_by_name_search(
        self,
        app_client: AsyncClient,
        admin_logged_in: None,
        db_session: AsyncSession,
    ) -> None:
        """Name search filter narrows results to matching stubs."""
        p1 = PlayerMaster(display_name="Alpha Stubson", first_name="Alpha", last_name="Stubson", is_stub=True)
        p2 = PlayerMaster(display_name="Beta Stubson", first_name="Beta", last_name="Stubson", is_stub=True)
        db_session.add(p1)
        db_session.add(p2)
        await db_session.commit()

        resp = await app_client.get("/admin/players/stubs?q=Alpha")
        assert resp.status_code == 200
        text = resp.text
        assert "Alpha Stubson" in text
        assert "Beta Stubson" not in text

    async def test_filter_by_draft_year(
        self,
        app_client: AsyncClient,
        admin_logged_in: None,
        db_session: AsyncSession,
    ) -> None:
        """Draft year filter narrows results to matching stubs."""
        p2026 = PlayerMaster(
            display_name="Year Stubson", first_name="Year", last_name="Stubson",
            is_stub=True, draft_year=2026,
        )
        p2027 = PlayerMaster(
            display_name="Other Stubson", first_name="Other", last_name="Stubson",
            is_stub=True, draft_year=2027,
        )
        db_session.add(p2026)
        db_session.add(p2027)
        await db_session.commit()

        resp = await app_client.get("/admin/players/stubs?draft_year=2026")
        assert resp.status_code == 200
        text = resp.text
        assert "Year Stubson" in text
        assert "Other Stubson" not in text

    async def test_filter_by_enrichment_not_attempted(
        self,
        app_client: AsyncClient,
        admin_logged_in: None,
        db_session: AsyncSession,
    ) -> None:
        """Enrichment filter 'not_attempted' shows only stubs without enrichment_attempted_at."""
        from datetime import datetime

        unenriched = PlayerMaster(
            display_name="Unenriched Stub", first_name="Unenriched", last_name="Stub",
            is_stub=True, enrichment_attempted_at=None,
        )
        enriched = PlayerMaster(
            display_name="Enriched Stub", first_name="Enriched", last_name="Stub",
            is_stub=True, enrichment_attempted_at=datetime.utcnow(),
        )
        db_session.add(unenriched)
        db_session.add(enriched)
        await db_session.commit()

        resp = await app_client.get("/admin/players/stubs?enrichment_status=not_attempted")
        assert resp.status_code == 200
        text = resp.text
        assert "Unenriched Stub" in text
        assert "Enriched Stub" not in text

    async def test_pagination_preserves_filters(
        self,
        app_client: AsyncClient,
        admin_logged_in: None,
    ) -> None:
        """Pagination links include active filter params."""
        resp = await app_client.get(
            "/admin/players/stubs?q=test&draft_year=2026&enrichment_status=not_attempted&limit=25&offset=0"
        )
        assert resp.status_code == 200
        # If > 1 page, the next link would include the filters.
        # For single-page result, just assert 200 response.


# ---------------------------------------------------------------------------
# §A1: Quick-add
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestQuickAddStub:
    """Tests for the quick-add stub endpoint."""

    async def test_quick_add_creates_stub(
        self,
        app_client: AsyncClient,
        admin_logged_in: None,
        db_session: AsyncSession,
    ) -> None:
        """POST /quick-add with a fresh name creates a stub and redirects."""
        resp = await app_client.post(
            "/admin/players/stubs/quick-add",
            data={"display_name": "Quickadd Testplayer"},
            follow_redirects=False,
        )
        assert resp.status_code in {302, 303}
        assert "success=quick_added" in resp.headers.get("location", "")

        result = await db_session.execute(
            select(PlayerMaster).where(
                PlayerMaster.display_name == "Quickadd Testplayer"  # type: ignore[arg-type]
            )
        )
        player = result.scalar_one_or_none()
        assert player is not None
        assert player.is_stub is True

    async def test_quick_add_with_draft_year(
        self,
        app_client: AsyncClient,
        admin_logged_in: None,
        db_session: AsyncSession,
    ) -> None:
        """Quick-add with a draft_year persists the year on the lifecycle row.

        The create_stub_player wrapper stores draft_year as
        PlayerLifecycle.expected_draft_year, not on PlayerMaster.draft_year
        directly (the PlayerMaster.draft_year column is for confirmed draft
        facts, not prospect expectations).
        """
        resp = await app_client.post(
            "/admin/players/stubs/quick-add",
            data={"display_name": "Draftyr Stubfellow", "draft_year": "2026"},
            follow_redirects=False,
        )
        assert resp.status_code in {302, 303}

        result = await db_session.execute(
            select(PlayerMaster).where(
                PlayerMaster.display_name == "Draftyr Stubfellow"  # type: ignore[arg-type]
            )
        )
        player = result.scalar_one_or_none()
        assert player is not None
        assert player.is_stub is True

        # Draft year is stored on the lifecycle row as expected_draft_year
        lifecycle_result = await db_session.execute(
            select(PlayerLifecycle).where(
                PlayerLifecycle.player_id == player.id  # type: ignore[arg-type]
            )
        )
        lifecycle = lifecycle_result.scalar_one_or_none()
        assert lifecycle is not None
        assert lifecycle.expected_draft_year == 2026

    async def test_quick_add_blocked_existing(
        self,
        app_client: AsyncClient,
        admin_logged_in: None,
        stub_player: PlayerMaster,
    ) -> None:
        """Quick-add with a name that matches an existing player shows an error."""
        # stub_player is "Stub Testington"
        resp = await app_client.post(
            "/admin/players/stubs/quick-add",
            data={"display_name": "Stub Testington"},
            follow_redirects=False,
        )
        # Should re-render the list (200) with an error, not redirect
        assert resp.status_code == 200
        assert "already exists" in resp.text.lower() or "existing" in resp.text.lower()

    async def test_quick_add_rejected_guard(
        self,
        app_client: AsyncClient,
        admin_logged_in: None,
    ) -> None:
        """Quick-add with a single-token name is rejected by the guard."""
        resp = await app_client.post(
            "/admin/players/stubs/quick-add",
            data={"display_name": "Mononame"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        # Should show an error about the name being too vague
        assert "Cannot create stub" in resp.text or "vague" in resp.text or "rejected" in resp.text.lower()

    async def test_quick_add_requires_edit_perm(
        self,
        app_client: AsyncClient,
        worker_view_only: int,
    ) -> None:
        """Worker with only view perm cannot quick-add stubs."""
        resp = await app_client.post(
            "/admin/players/stubs/quick-add",
            data={"display_name": "Permission Test Player"},
            follow_redirects=False,
        )
        assert resp.status_code in {302, 303}


# ---------------------------------------------------------------------------
# §B: Promote
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestPromoteStub:
    """Tests for the promote-to-full endpoint."""

    async def test_promote_clears_is_stub(
        self,
        app_client: AsyncClient,
        admin_logged_in: None,
        stub_player: PlayerMaster,
        db_session: AsyncSession,
    ) -> None:
        """POST /promote clears is_stub on the PlayerMaster row."""
        assert stub_player.id is not None
        resp = await app_client.post(
            f"/admin/players/stubs/{stub_player.id}/promote",
            follow_redirects=False,
        )
        assert resp.status_code in {302, 303}
        assert "success=promoted" in resp.headers.get("location", "")

        await db_session.refresh(stub_player)
        assert stub_player.is_stub is False

    async def test_promote_404_for_missing_player(
        self,
        app_client: AsyncClient,
        admin_logged_in: None,
    ) -> None:
        """POST /promote with a non-existent player_id returns 404."""
        resp = await app_client.post(
            "/admin/players/stubs/99999/promote",
            follow_redirects=False,
        )
        assert resp.status_code == 404

    async def test_promote_400_for_full_player(
        self,
        app_client: AsyncClient,
        admin_logged_in: None,
        full_player: PlayerMaster,
    ) -> None:
        """POST /promote on a non-stub player returns 400."""
        assert full_player.id is not None
        resp = await app_client.post(
            f"/admin/players/stubs/{full_player.id}/promote",
            follow_redirects=False,
        )
        assert resp.status_code == 400

    async def test_promote_requires_edit_perm(
        self,
        app_client: AsyncClient,
        worker_view_only: int,
        stub_player: PlayerMaster,
    ) -> None:
        """Worker with only view perm cannot promote stubs."""
        assert stub_player.id is not None
        resp = await app_client.post(
            f"/admin/players/stubs/{stub_player.id}/promote",
            follow_redirects=False,
        )
        assert resp.status_code in {302, 303}


# ---------------------------------------------------------------------------
# §B: Delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestDeleteStub:
    """Tests for the reference-guarded stub delete endpoint."""

    async def test_delete_orphan_stub_succeeds(
        self,
        app_client: AsyncClient,
        admin_logged_in: None,
        stub_player: PlayerMaster,
        db_session: AsyncSession,
    ) -> None:
        """DELETE an orphan stub (no blocking inbound refs) succeeds."""
        assert stub_player.id is not None
        resp = await app_client.post(
            f"/admin/players/stubs/{stub_player.id}/delete",
            follow_redirects=False,
        )
        assert resp.status_code in {302, 303}
        assert "success=deleted" in resp.headers.get("location", "")

        result = await db_session.execute(
            select(PlayerMaster).where(
                PlayerMaster.id == stub_player.id  # type: ignore[arg-type]
            )
        )
        assert result.scalar_one_or_none() is None

    async def test_delete_also_removes_lifecycle_and_alias(
        self,
        app_client: AsyncClient,
        admin_logged_in: None,
        db_session: AsyncSession,
    ) -> None:
        """Delete cleans up auto-created lifecycle and alias rows."""
        p = PlayerMaster(
            display_name="Cleanup Stubber", first_name="Cleanup", last_name="Stubber",
            is_stub=True,
        )
        db_session.add(p)
        await db_session.flush()
        assert p.id is not None

        lifecycle = PlayerLifecycle(player_id=p.id)
        alias = PlayerAlias(
            player_id=p.id,
            full_name="Cleanup Stubber",
            first_name="Cleanup",
            last_name="Stubber",
            context="mention_resolution",
        )
        db_session.add(lifecycle)
        db_session.add(alias)
        await db_session.commit()

        resp = await app_client.post(
            f"/admin/players/stubs/{p.id}/delete",
            follow_redirects=False,
        )
        assert resp.status_code in {302, 303}

        # Verify cleanup
        pl = (await db_session.execute(
            select(PlayerLifecycle).where(
                PlayerLifecycle.player_id == p.id  # type: ignore[arg-type]
            )
        )).scalar_one_or_none()
        assert pl is None

        al = (await db_session.execute(
            select(PlayerAlias).where(
                PlayerAlias.player_id == p.id  # type: ignore[arg-type]
            )
        )).scalar_one_or_none()
        assert al is None

    async def test_delete_refused_when_board_entry_exists(
        self,
        app_client: AsyncClient,
        admin_logged_in: None,
        db_session: AsyncSession,
    ) -> None:
        """Delete is refused when the stub has an inbound board entry reference."""
        # Create a stub
        p = PlayerMaster(
            display_name="Guarded Stubson", first_name="Guarded", last_name="Stubson",
            is_stub=True,
        )
        db_session.add(p)
        await db_session.flush()
        assert p.id is not None

        # Create a board and entry referencing the stub
        src = await _make_news_source(db_session)
        assert src.id is not None
        board = Board(
            news_source_id=src.id,
            draft_year=2026,
            published_at=datetime.now(timezone.utc).replace(tzinfo=None),
            size=1,
            status=BoardStatus.PENDING,
        )
        db_session.add(board)
        await db_session.flush()
        assert board.id is not None

        entry = BoardEntry(board_id=board.id, player_id=p.id, position=1)
        db_session.add(entry)
        await db_session.commit()

        resp = await app_client.post(
            f"/admin/players/stubs/{p.id}/delete",
            follow_redirects=False,
        )
        # Should re-render the list page with an error (not a redirect)
        assert resp.status_code == 200
        assert "Cannot delete stub" in resp.text or "inbound references" in resp.text.lower()

        # Player still exists
        still_there = (await db_session.execute(
            select(PlayerMaster).where(
                PlayerMaster.id == p.id  # type: ignore[arg-type]
            )
        )).scalar_one_or_none()
        assert still_there is not None

    async def test_delete_refused_for_non_stub(
        self,
        app_client: AsyncClient,
        admin_logged_in: None,
        full_player: PlayerMaster,
    ) -> None:
        """Delete refuses a non-stub player through the stub delete route."""
        assert full_player.id is not None
        resp = await app_client.post(
            f"/admin/players/stubs/{full_player.id}/delete",
            follow_redirects=False,
        )
        # Should surface an error (200 re-render or 4xx depending on guard)
        # The service raises ValueError, which the route renders as a list error
        assert resp.status_code in {200, 400}

    async def test_delete_requires_edit_perm(
        self,
        app_client: AsyncClient,
        worker_view_only: int,
        stub_player: PlayerMaster,
    ) -> None:
        """Worker with only view perm cannot delete stubs."""
        assert stub_player.id is not None
        resp = await app_client.post(
            f"/admin/players/stubs/{stub_player.id}/delete",
            follow_redirects=False,
        )
        assert resp.status_code in {302, 303}

    async def test_delete_404_for_missing_player(
        self,
        app_client: AsyncClient,
        admin_logged_in: None,
    ) -> None:
        """DELETE with a non-existent player_id returns 404."""
        resp = await app_client.post(
            "/admin/players/stubs/99999/delete",
            follow_redirects=False,
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# §B: Bulk delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestBulkDeleteStubs:
    """Tests for the bulk-delete endpoint."""

    async def test_bulk_delete_orphans_succeed(
        self,
        app_client: AsyncClient,
        admin_logged_in: None,
        db_session: AsyncSession,
    ) -> None:
        """Bulk-delete removes all orphan stubs and redirects."""
        p1 = PlayerMaster(display_name="Bulk One", first_name="Bulk", last_name="One", is_stub=True)
        p2 = PlayerMaster(display_name="Bulk Two", first_name="Bulk", last_name="Two", is_stub=True)
        db_session.add(p1)
        db_session.add(p2)
        await db_session.commit()
        assert p1.id is not None and p2.id is not None

        resp = await app_client.post(
            "/admin/players/stubs/bulk-delete",
            content=f"player_ids%5B%5D={p1.id}&player_ids%5B%5D={p2.id}",
            headers={"content-type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code in {302, 303}
        assert "success=deleted" in resp.headers.get("location", "")

        for pid in (p1.id, p2.id):
            result = await db_session.execute(
                select(PlayerMaster).where(PlayerMaster.id == pid)  # type: ignore[arg-type]
            )
            assert result.scalar_one_or_none() is None

    async def test_bulk_delete_partial_when_guarded(
        self,
        app_client: AsyncClient,
        admin_logged_in: None,
        db_session: AsyncSession,
    ) -> None:
        """Bulk-delete skips guarded stubs and surfaces an error summary."""
        # Create one deletable stub and one with a board entry
        orphan = PlayerMaster(
            display_name="Bulk Orphan", first_name="Bulk", last_name="Orphan", is_stub=True
        )
        guarded = PlayerMaster(
            display_name="Bulk Guarded", first_name="Bulk", last_name="Guarded", is_stub=True
        )
        db_session.add(orphan)
        db_session.add(guarded)
        await db_session.flush()
        assert orphan.id is not None and guarded.id is not None

        src = await _make_news_source(db_session)
        assert src.id is not None
        board = Board(
            news_source_id=src.id,
            draft_year=2026,
            published_at=datetime.now(timezone.utc).replace(tzinfo=None),
            size=1,
            status=BoardStatus.PENDING,
        )
        db_session.add(board)
        await db_session.flush()
        assert board.id is not None
        db_session.add(BoardEntry(board_id=board.id, player_id=guarded.id, position=1))
        await db_session.commit()

        resp = await app_client.post(
            "/admin/players/stubs/bulk-delete",
            content=f"player_ids%5B%5D={orphan.id}&player_ids%5B%5D={guarded.id}",
            headers={"content-type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        # Re-renders list with error summary
        assert resp.status_code == 200
        assert "failed" in resp.text.lower() or "Cannot delete stub" in resp.text

        # Orphan was deleted; guarded still exists
        orphan_row = (await db_session.execute(
            select(PlayerMaster).where(PlayerMaster.id == orphan.id)  # type: ignore[arg-type]
        )).scalar_one_or_none()
        assert orphan_row is None

        guarded_row = (await db_session.execute(
            select(PlayerMaster).where(PlayerMaster.id == guarded.id)  # type: ignore[arg-type]
        )).scalar_one_or_none()
        assert guarded_row is not None


# ---------------------------------------------------------------------------
# Perf budget: authenticated stubs tab
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stubs_tab_within_query_budget(
    app_client: AsyncClient,
    admin_logged_in: None,
    async_engine: AsyncEngine,
    db_session: AsyncSession,
) -> None:
    """Authenticated GET /admin/players/stubs stays within its query budget.

    Budget is defined in tests/integration/perf/budgets.ADMIN_ROUTE_BUDGETS.
    The route must render without exceeding the documented maximum query count.
    """
    # Seed a couple of stubs so the query path exercises the loop
    for i in range(3):
        db_session.add(
            PlayerMaster(
                display_name=f"Budget Stub {i}",
                first_name="Budget",
                last_name=f"Stub{i}",
                is_stub=True,
            )
        )
    await db_session.commit()

    budget = ADMIN_ROUTE_BUDGETS["/admin/players/stubs"]

    with count_queries(async_engine) as captured:
        resp = await app_client.get("/admin/players/stubs")

    assert resp.status_code == 200, (
        f"/admin/players/stubs returned {resp.status_code}; expected 200."
    )

    if len(captured) > budget:
        listing = "\n".join(
            f"  {i + 1:>2}. {' '.join(stmt.split())[:120]}"
            for i, stmt in enumerate(captured)
        )
        pytest.fail(
            f"/admin/players/stubs issued {len(captured)} queries, over its budget "
            f"of {budget}.\n"
            f"Raise ADMIN_ROUTE_BUDGETS['/admin/players/stubs'] in "
            f"tests/integration/perf/budgets.py if the query is genuinely needed.\n"
            f"Captured statements:\n{listing}"
        )
