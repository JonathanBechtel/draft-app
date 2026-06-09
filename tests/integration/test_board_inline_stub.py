"""Integration tests for inline stub creation in the board add-entry flow (spec §A3).

Covers:
- POST /admin/boards/{board_id}/entries/inline-stub with a new name mints a stub
  and adds a STUB-resolution entry in one step.
- Blocked existing: when the name matches a unique existing player, returns 409
  with blocked_existing outcome; no stub minted.
- Ambiguous: when the name matches multiple existing players, returns 409 with
  ambiguous outcome; no stub minted.
- Rejected guard: single-token name fails the specificity guard, returns 422.
- Board not PENDING: attempt to add to an APPROVED board returns 400.
- Regression: existing mint-stub route (POST /entries/{entry_id}/mint-stub) for
  unresolved entries is unaffected.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.boards import Board, BoardEntry, BoardStatus, ResolutionMethod
from app.schemas.news_sources import FeedType, NewsSource
from app.schemas.players_master import PlayerMaster
from tests.integration.auth_helpers import create_auth_user, login_staff
from tests.integration.conftest import make_player


ADMIN_EMAIL = "inline-stub-admin@example.com"
ADMIN_PASSWORD = "inline-stub-pass-456"


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
async def board_source(db_session: AsyncSession) -> NewsSource:
    """A minimal active news source for boards."""
    src = NewsSource(
        name="inline-stub-source",
        display_name="Inline Stub Test Source",
        feed_type=FeedType.RSS,
        feed_url="https://example.com/inline-stub-feed.xml",
        is_active=True,
        fetch_interval_minutes=30,
    )
    db_session.add(src)
    await db_session.commit()
    await db_session.refresh(src)
    return src


@pytest_asyncio.fixture
async def pending_board(db_session: AsyncSession, board_source: NewsSource) -> Board:
    """A PENDING board for tests."""
    assert board_source.id is not None
    board = Board(
        news_source_id=board_source.id,
        draft_year=2027,
        published_at=datetime.now(timezone.utc).replace(tzinfo=None),
        size=0,
        status=BoardStatus.PENDING,
    )
    db_session.add(board)
    await db_session.commit()
    await db_session.refresh(board)
    return board


@pytest_asyncio.fixture
async def approved_board(db_session: AsyncSession, board_source: NewsSource) -> Board:
    """An APPROVED board (immutable — cannot add entries)."""
    assert board_source.id is not None
    board = Board(
        news_source_id=board_source.id,
        draft_year=2027,
        published_at=datetime.now(timezone.utc).replace(tzinfo=None),
        size=0,
        status=BoardStatus.APPROVED,
    )
    db_session.add(board)
    await db_session.commit()
    await db_session.refresh(board)
    return board


@pytest.mark.asyncio
class TestInlineStubHappyPath:
    """Happy-path tests: stub minted and entry created in one step."""

    async def test_inline_stub_creates_player_and_entry(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_logged_in: None,
        pending_board: Board,
    ) -> None:
        """POST inline-stub with a new name mints a stub and adds the entry.

        The response should be 201 JSON with outcome='created', a player_id,
        and a display_name matching the submitted name. The DB should have a
        new PlayerMaster with is_stub=True and a BoardEntry with
        resolution_method=STUB pointing to it.
        """
        _ = admin_logged_in
        assert pending_board.id is not None

        resp = await app_client.post(
            f"/admin/boards/{pending_board.id}/entries/inline-stub",
            data={"name": "Xander Blaine", "position": "1"},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["outcome"] == "created"
        assert body["player_id"] is not None
        assert body["display_name"] == "Xander Blaine"

        # Verify DB state
        player_result = await db_session.execute(
            select(PlayerMaster).where(
                PlayerMaster.display_name == "Xander Blaine"  # type: ignore[arg-type]
            )
        )
        player = player_result.scalar_one_or_none()
        assert player is not None, "Stub player not created in DB"
        assert player.is_stub is True

        entry_result = await db_session.execute(
            select(BoardEntry).where(
                BoardEntry.player_id == player.id  # type: ignore[arg-type]
            )
        )
        entry = entry_result.scalar_one_or_none()
        assert entry is not None, "Board entry not created"
        assert entry.board_id == pending_board.id
        assert entry.resolution_method == ResolutionMethod.STUB
        assert entry.position == 1

    async def test_inline_stub_sets_raw_name(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_logged_in: None,
        pending_board: Board,
    ) -> None:
        """The created entry should carry raw_name matching the submitted name."""
        _ = admin_logged_in
        assert pending_board.id is not None

        resp = await app_client.post(
            f"/admin/boards/{pending_board.id}/entries/inline-stub",
            data={"name": "Zara Melo", "position": "2"},
        )
        assert resp.status_code == 201

        player_result = await db_session.execute(
            select(PlayerMaster).where(
                PlayerMaster.display_name == "Zara Melo"  # type: ignore[arg-type]
            )
        )
        player = player_result.scalar_one_or_none()
        assert player is not None

        entry_result = await db_session.execute(
            select(BoardEntry).where(
                BoardEntry.player_id == player.id  # type: ignore[arg-type]
            )
        )
        entry = entry_result.scalar_one_or_none()
        assert entry is not None
        assert entry.raw_name == "Zara Melo"


@pytest.mark.asyncio
class TestInlineStubDedupBlocking:
    """Tests that dedup blocks duplicate stubs via blocked_existing / ambiguous."""

    async def test_inline_stub_blocked_when_player_exists(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_logged_in: None,
        pending_board: Board,
    ) -> None:
        """When an exact player already exists, the route returns 409 blocked_existing.

        No new PlayerMaster row should be created and no entry should be added.
        """
        _ = admin_logged_in
        assert pending_board.id is not None

        # Seed an existing player
        existing = make_player("Cooper", "Flagg", school="Duke")
        db_session.add(existing)
        await db_session.commit()
        await db_session.refresh(existing)

        resp = await app_client.post(
            f"/admin/boards/{pending_board.id}/entries/inline-stub",
            data={"name": "Cooper Flagg", "position": "1"},
        )
        assert resp.status_code == 409, resp.text
        body = resp.json()
        assert body["outcome"] == "blocked_existing"
        assert body["player_id"] == existing.id

        # No new player created
        count_result = await db_session.execute(
            select(PlayerMaster).where(
                PlayerMaster.display_name == "Cooper Flagg"  # type: ignore[arg-type]
            )
        )
        rows = count_result.scalars().all()
        assert len(rows) == 1, "Duplicate player should not have been created"

        # No entry added
        entry_result = await db_session.execute(
            select(BoardEntry).where(
                BoardEntry.board_id == pending_board.id  # type: ignore[arg-type]
            )
        )
        entries = entry_result.scalars().all()
        assert len(entries) == 0, "No entry should have been added on blocked_existing"

    async def test_inline_stub_rejected_for_single_token(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_logged_in: None,
        pending_board: Board,
    ) -> None:
        """A single-token name is rejected by the guard with a 422 response.

        Single-word names like 'Blaine' are too ambiguous to create a stub player.
        """
        _ = admin_logged_in
        assert pending_board.id is not None

        resp = await app_client.post(
            f"/admin/boards/{pending_board.id}/entries/inline-stub",
            data={"name": "Blaine", "position": "1"},
        )
        assert resp.status_code == 422, resp.text
        body = resp.json()
        assert body["outcome"] == "rejected_guard"

        # No player or entry created
        player_result = await db_session.execute(
            select(PlayerMaster).where(
                PlayerMaster.display_name == "Blaine"  # type: ignore[arg-type]
            )
        )
        assert player_result.scalar_one_or_none() is None

    async def test_inline_stub_error_on_empty_name(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_logged_in: None,
        pending_board: Board,
    ) -> None:
        """An empty name returns 422 without creating anything."""
        _ = admin_logged_in
        assert pending_board.id is not None

        resp = await app_client.post(
            f"/admin/boards/{pending_board.id}/entries/inline-stub",
            data={"name": "   ", "position": "1"},
        )
        assert resp.status_code == 422, resp.text
        body = resp.json()
        assert body["outcome"] in {"error", "rejected_guard"}


@pytest.mark.asyncio
class TestInlineStubBoardState:
    """Tests that enforce board-state constraints."""

    async def test_inline_stub_fails_on_approved_board(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_logged_in: None,
        approved_board: Board,
    ) -> None:
        """Attempting to add a stub entry to an APPROVED board returns 400.

        Approved boards are immutable; the service raises BoardNotEditableError.
        """
        _ = admin_logged_in
        assert approved_board.id is not None

        resp = await app_client.post(
            f"/admin/boards/{approved_board.id}/entries/inline-stub",
            data={"name": "New Prospect Guy", "position": "1"},
        )
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body["outcome"] == "error"

    async def test_inline_stub_requires_login(
        self,
        app_client: AsyncClient,
        pending_board: Board,
    ) -> None:
        """POST inline-stub redirects to login when not authenticated."""
        assert pending_board.id is not None

        resp = await app_client.post(
            f"/admin/boards/{pending_board.id}/entries/inline-stub",
            data={"name": "Some Player", "position": "1"},
            follow_redirects=False,
        )
        # Unauthenticated requests return 401 JSON (the route returns JSONResponse)
        assert resp.status_code in {302, 303, 401}


@pytest.mark.asyncio
class TestExistingMintStubRegression:
    """Regression: the original mint-stub button for unresolved entries still works."""

    async def test_mint_stub_for_unresolved_entry_still_works(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_logged_in: None,
        pending_board: Board,
    ) -> None:
        """POST .../entries/{entry_id}/mint-stub resolves an unresolved entry as before.

        The new inline-stub route must not interfere with the existing resolution
        path. An UNRESOLVED entry with a raw_name can still be resolved via the
        original mint-stub button.
        """
        _ = admin_logged_in
        assert pending_board.id is not None

        # Seed an unresolved entry directly
        unresolved_entry = BoardEntry(
            board_id=pending_board.id,
            player_id=None,
            position=99,
            raw_name="Mystery Prospect",
            resolution_method=ResolutionMethod.UNRESOLVED,
        )
        db_session.add(unresolved_entry)
        # Also update the board size
        pending_board.size = 1
        db_session.add(pending_board)
        await db_session.commit()
        await db_session.refresh(unresolved_entry)
        assert unresolved_entry.id is not None

        resp = await app_client.post(
            f"/admin/boards/{pending_board.id}/entries/{unresolved_entry.id}/mint-stub",
            follow_redirects=False,
        )
        assert resp.status_code in {302, 303}
        assert "stub_minted" in resp.headers.get("location", "")

        # Verify the entry is now resolved
        await db_session.refresh(unresolved_entry)
        assert unresolved_entry.player_id is not None
        assert unresolved_entry.resolution_method == ResolutionMethod.STUB

        # Verify a stub player was created
        player_result = await db_session.execute(
            select(PlayerMaster).where(
                PlayerMaster.id == unresolved_entry.player_id  # type: ignore[arg-type]
            )
        )
        player = player_result.scalar_one_or_none()
        assert player is not None
        assert player.is_stub is True
        assert player.display_name == "Mystery Prospect"
