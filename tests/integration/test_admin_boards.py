"""HTTP-level integration tests for the admin big-boards routes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.boards import Board, BoardEntry, BoardStatus
from app.schemas.news_sources import FeedType, NewsSource
from app.schemas.players_master import PlayerMaster
from tests.integration.auth_helpers import create_auth_user, login_staff
from tests.integration.conftest import make_player


ADMIN_EMAIL = "bigboard-admin@example.com"
ADMIN_PASSWORD = "admin-password-123"


@pytest_asyncio.fixture
async def admin_logged_in(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Create an admin user and log into ``app_client`` so the session cookie is set."""
    await create_auth_user(
        db_session,
        email=ADMIN_EMAIL,
        role="admin",
        password=ADMIN_PASSWORD,
    )
    await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)


@pytest_asyncio.fixture
async def big_board_source(db_session: AsyncSession) -> NewsSource:
    """A unique active news source available to the new-board form."""
    source = NewsSource(
        name="big-board-source",
        display_name="Big Board Test Source",
        feed_type=FeedType.RSS,
        feed_url="https://example.com/bb-test-feed.xml",
        is_active=True,
        fetch_interval_minutes=30,
    )
    db_session.add(source)
    await db_session.commit()
    await db_session.refresh(source)
    assert source.id is not None
    return source


@pytest_asyncio.fixture
async def sample_players(db_session: AsyncSession) -> list[PlayerMaster]:
    rows = [
        make_player("Cooper", "Flagg", school="Duke"),
        make_player("Dylan", "Harper", school="Rutgers"),
        make_player("Ace", "Bailey", school="Rutgers"),
    ]
    for p in rows:
        db_session.add(p)
    await db_session.commit()
    for p in rows:
        await db_session.refresh(p)
    return rows


@pytest.mark.asyncio
class TestBigBoardAccess:
    """Access control: unauthenticated users are redirected to login."""

    async def test_list_requires_login(self, app_client: AsyncClient):
        """GET /admin/boards redirects when logged out."""
        response = await app_client.get(
            "/admin/boards", follow_redirects=False
        )
        assert response.status_code in {302, 303}
        assert "/admin/login" in response.headers.get("location", "")

    async def test_create_requires_login(self, app_client: AsyncClient):
        """POST /admin/boards redirects when logged out."""
        response = await app_client.post(
            "/admin/boards",
            data={
                "news_source_id": "1",
                "draft_year": "2026",
                "published_at": "2026-05-01",
            },
            follow_redirects=False,
        )
        assert response.status_code in {302, 303}
        assert "/admin/login" in response.headers.get("location", "")


@pytest.mark.asyncio
class TestBigBoardLifecycle:
    """End-to-end flow: create, add entry, edit entry, approve, immutability."""

    async def test_full_create_approve_flow(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_logged_in: None,
        big_board_source: NewsSource,
        sample_players: list[PlayerMaster],
    ):
        """Create empty board, add two entries, edit one, approve, confirm DB state."""
        _ = admin_logged_in
        assert big_board_source.id is not None

        # 1. Create board
        create_resp = await app_client.post(
            "/admin/boards",
            data={
                "news_source_id": str(big_board_source.id),
                "draft_year": "2026",
                "published_at": "2026-05-01",
            },
            follow_redirects=False,
        )
        assert create_resp.status_code == 303
        location = create_resp.headers["location"]
        assert location.startswith("/admin/boards/")
        board_id = int(location.split("/")[-1].split("?")[0])

        board_row = await db_session.get(Board, board_id)
        assert board_row is not None
        assert board_row.status is BoardStatus.PENDING
        assert board_row.size == 0

        # 2. Add first entry
        first_player_id = sample_players[0].id
        add_resp = await app_client.post(
            f"/admin/boards/{board_id}/entries",
            data={
                "player_id": str(first_player_id),
                "position": "1",
                "tier": "1",
            },
            follow_redirects=False,
        )
        assert add_resp.status_code == 303

        # 3. Add second entry
        second_player_id = sample_players[1].id
        add_resp_2 = await app_client.post(
            f"/admin/boards/{board_id}/entries",
            data={
                "player_id": str(second_player_id),
                "position": "2",
            },
            follow_redirects=False,
        )
        assert add_resp_2.status_code == 303

        # Verify size + entries
        await db_session.refresh(board_row)
        assert board_row.size == 2
        entries = (
            await db_session.execute(
                select(BoardEntry)  # type: ignore[call-overload]
                .where(BoardEntry.board_id == board_id)  # type: ignore[arg-type]
                .order_by(BoardEntry.position)  # type: ignore[arg-type]
            )
        ).scalars().all()
        assert [(e.player_id, e.position, e.tier) for e in entries] == [
            (first_player_id, 1, 1),
            (second_player_id, 2, None),
        ]

        # 4. Edit the second entry (set tier=2)
        edit_resp = await app_client.post(
            f"/admin/boards/{board_id}/entries/{entries[1].id}/update",
            data={"position": "2", "tier": "2"},
            follow_redirects=False,
        )
        assert edit_resp.status_code == 303
        edited_id = entries[1].id
        edited = await db_session.get(
            BoardEntry, edited_id, populate_existing=True
        )
        assert edited is not None
        assert edited.tier == 2

        # 5. Approve
        approve_resp = await app_client.post(
            f"/admin/boards/{board_id}/approve", follow_redirects=False
        )
        assert approve_resp.status_code == 303
        await db_session.refresh(board_row)
        assert board_row.status is BoardStatus.APPROVED
        assert board_row.approved_at is not None

        # 6. Post-approval edits redirect with an error and do not mutate
        post_approve_resp = await app_client.post(
            f"/admin/boards/{board_id}/entries",
            data={
                "player_id": str(sample_players[2].id),
                "position": "3",
            },
            follow_redirects=False,
        )
        assert post_approve_resp.status_code == 303
        assert "error=" in post_approve_resp.headers["location"]
        await db_session.refresh(board_row)
        assert board_row.size == 2

    async def test_reject_flow_preserves_row_for_audit(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_logged_in: None,
        big_board_source: NewsSource,
        sample_players: list[PlayerMaster],
    ):
        """Rejection locks the board but leaves the row in place."""
        _ = admin_logged_in
        assert big_board_source.id is not None

        create_resp = await app_client.post(
            "/admin/boards",
            data={
                "news_source_id": str(big_board_source.id),
                "draft_year": "2026",
                "published_at": "2026-05-01",
            },
            follow_redirects=False,
        )
        board_id = int(
            create_resp.headers["location"].split("/")[-1].split("?")[0]
        )

        # Add one entry so reject is non-trivial
        await app_client.post(
            f"/admin/boards/{board_id}/entries",
            data={
                "player_id": str(sample_players[0].id),
                "position": "1",
            },
            follow_redirects=False,
        )

        reject_resp = await app_client.post(
            f"/admin/boards/{board_id}/reject", follow_redirects=False
        )
        assert reject_resp.status_code == 303

        board_row = await db_session.get(Board, board_id)
        assert board_row is not None
        assert board_row.status is BoardStatus.REJECTED
        assert board_row.approved_at is None

        # Approve a rejected board should error
        approve_again = await app_client.post(
            f"/admin/boards/{board_id}/approve", follow_redirects=False
        )
        assert approve_again.status_code == 303
        assert "error=" in approve_again.headers["location"]

    async def test_detail_page_includes_current_source_when_deactivated(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_logged_in: None,
        big_board_source: NewsSource,
    ):
        """If the board's source was deactivated, the edit dropdown still lists it.

        Otherwise a year/date-only edit on a PENDING board would silently
        reassign attribution to whichever active source the required
        <select> defaulted to.
        """
        _ = admin_logged_in
        assert big_board_source.id is not None

        create_resp = await app_client.post(
            "/admin/boards",
            data={
                "news_source_id": str(big_board_source.id),
                "draft_year": "2026",
                "published_at": "2026-05-01",
            },
            follow_redirects=False,
        )
        board_id = int(
            create_resp.headers["location"].split("/")[-1].split("?")[0]
        )

        # Deactivate the source that the board was created against.
        src_row = await db_session.get(
            NewsSource, big_board_source.id, populate_existing=True
        )
        assert src_row is not None
        src_row.is_active = False
        await db_session.commit()

        # Add an extra active source so the dropdown isn't empty otherwise.
        other = NewsSource(
            name="other-active",
            display_name="Other Active Source",
            feed_type=FeedType.RSS,
            feed_url="https://example.com/other-active-feed.xml",
            is_active=True,
            fetch_interval_minutes=30,
        )
        db_session.add(other)
        await db_session.commit()

        detail = await app_client.get(f"/admin/boards/{board_id}")
        assert detail.status_code == 200
        # The deactivated current source is still rendered as an option,
        # tagged "(inactive)" so the admin sees its status.
        assert big_board_source.display_name in detail.text
        assert "(inactive)" in detail.text
        # And it's the selected option (the required select hasn't silently
        # defaulted to the other active source).
        assert (
            f'value="{big_board_source.id}" selected'
            in detail.text.replace("'", '"')
        )

    async def test_update_meta_changes_source_year_published_at(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_logged_in: None,
        big_board_source: NewsSource,
    ):
        """POST /update-meta patches source/year/published_at on a PENDING board."""
        _ = admin_logged_in
        assert big_board_source.id is not None

        other_source = NewsSource(
            name="alt-meta-source",
            display_name="Alt Meta Source",
            feed_type=FeedType.RSS,
            feed_url="https://example.com/alt-meta-feed.xml",
            is_active=True,
            fetch_interval_minutes=30,
        )
        db_session.add(other_source)
        await db_session.commit()
        await db_session.refresh(other_source)
        assert other_source.id is not None

        create_resp = await app_client.post(
            "/admin/boards",
            data={
                "news_source_id": str(big_board_source.id),
                "draft_year": "2026",
                "published_at": "2026-05-01",
            },
            follow_redirects=False,
        )
        board_id = int(
            create_resp.headers["location"].split("/")[-1].split("?")[0]
        )

        update_resp = await app_client.post(
            f"/admin/boards/{board_id}/update-meta",
            data={
                "news_source_id": str(other_source.id),
                "draft_year": "2027",
                "published_at": "2026-05-10",
            },
            follow_redirects=False,
        )
        assert update_resp.status_code == 303
        assert "success=meta_updated" in update_resp.headers["location"]

        board_row = await db_session.get(
            Board, board_id, populate_existing=True
        )
        assert board_row is not None
        assert board_row.news_source_id == other_source.id
        assert board_row.draft_year == 2027
        assert board_row.published_at.strftime("%Y-%m-%d") == "2026-05-10"

    async def test_update_meta_rejected_on_approved_board(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_logged_in: None,
        big_board_source: NewsSource,
        sample_players: list[PlayerMaster],
    ):
        """Editing metadata on an APPROVED board redirects with an error."""
        _ = admin_logged_in
        assert big_board_source.id is not None

        create_resp = await app_client.post(
            "/admin/boards",
            data={
                "news_source_id": str(big_board_source.id),
                "draft_year": "2026",
                "published_at": "2026-05-01",
            },
            follow_redirects=False,
        )
        board_id = int(
            create_resp.headers["location"].split("/")[-1].split("?")[0]
        )
        await app_client.post(
            f"/admin/boards/{board_id}/entries",
            data={"player_id": str(sample_players[0].id), "position": "1"},
            follow_redirects=False,
        )
        await app_client.post(
            f"/admin/boards/{board_id}/approve", follow_redirects=False
        )

        update_resp = await app_client.post(
            f"/admin/boards/{board_id}/update-meta",
            data={
                "news_source_id": str(big_board_source.id),
                "draft_year": "2027",
                "published_at": "2026-05-10",
            },
            follow_redirects=False,
        )
        assert update_resp.status_code == 303
        assert "error=" in update_resp.headers["location"]

        board_row = await db_session.get(
            Board, board_id, populate_existing=True
        )
        assert board_row is not None
        assert board_row.draft_year == 2026  # unchanged

    async def test_reopen_flips_approved_back_to_pending(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_logged_in: None,
        big_board_source: NewsSource,
        sample_players: list[PlayerMaster],
    ):
        """POST /reopen unlocks an APPROVED board for edits and clears approved_at."""
        _ = admin_logged_in
        assert big_board_source.id is not None

        create_resp = await app_client.post(
            "/admin/boards",
            data={
                "news_source_id": str(big_board_source.id),
                "draft_year": "2026",
                "published_at": "2026-05-01",
            },
            follow_redirects=False,
        )
        board_id = int(
            create_resp.headers["location"].split("/")[-1].split("?")[0]
        )
        await app_client.post(
            f"/admin/boards/{board_id}/entries",
            data={"player_id": str(sample_players[0].id), "position": "1"},
            follow_redirects=False,
        )
        await app_client.post(
            f"/admin/boards/{board_id}/approve", follow_redirects=False
        )

        reopen_resp = await app_client.post(
            f"/admin/boards/{board_id}/reopen", follow_redirects=False
        )
        assert reopen_resp.status_code == 303
        assert "success=reopened" in reopen_resp.headers["location"]

        board_row = await db_session.get(
            Board, board_id, populate_existing=True
        )
        assert board_row is not None
        assert board_row.status is BoardStatus.PENDING
        assert board_row.approved_at is None

        # Re-adding entries should now work
        add_resp = await app_client.post(
            f"/admin/boards/{board_id}/entries",
            data={"player_id": str(sample_players[1].id), "position": "2"},
            follow_redirects=False,
        )
        assert add_resp.status_code == 303
        assert "success=entry_added" in add_resp.headers["location"]

    async def test_clone_creates_pending_copy_with_same_entries(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_logged_in: None,
        big_board_source: NewsSource,
        sample_players: list[PlayerMaster],
    ):
        """POST /clone creates a fresh PENDING board with the same entries."""
        _ = admin_logged_in
        assert big_board_source.id is not None

        create_resp = await app_client.post(
            "/admin/boards",
            data={
                "news_source_id": str(big_board_source.id),
                "draft_year": "2026",
                "published_at": "2026-05-01",
            },
            follow_redirects=False,
        )
        board_id = int(
            create_resp.headers["location"].split("/")[-1].split("?")[0]
        )
        await app_client.post(
            f"/admin/boards/{board_id}/entries",
            data={
                "player_id": str(sample_players[0].id),
                "position": "1",
                "tier": "1",
            },
            follow_redirects=False,
        )
        await app_client.post(
            f"/admin/boards/{board_id}/entries",
            data={"player_id": str(sample_players[1].id), "position": "2"},
            follow_redirects=False,
        )

        clone_resp = await app_client.post(
            f"/admin/boards/{board_id}/clone",
            data={"published_at": "2026-05-20"},
            follow_redirects=False,
        )
        assert clone_resp.status_code == 303
        clone_id = int(
            clone_resp.headers["location"].split("/")[-1].split("?")[0]
        )
        assert clone_id != board_id

        clone_row = await db_session.get(Board, clone_id)
        assert clone_row is not None
        assert clone_row.status is BoardStatus.PENDING
        assert clone_row.size == 2

        entries = (
            await db_session.execute(
                select(BoardEntry)  # type: ignore[call-overload]
                .where(BoardEntry.board_id == clone_id)  # type: ignore[arg-type]
                .order_by(BoardEntry.position)  # type: ignore[arg-type]
            )
        ).scalars().all()
        assert [(e.player_id, e.position, e.tier) for e in entries] == [
            (sample_players[0].id, 1, 1),
            (sample_players[1].id, 2, None),
        ]

    async def test_move_up_and_down_swap_ranks(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_logged_in: None,
        big_board_source: NewsSource,
        sample_players: list[PlayerMaster],
    ):
        """POST /move-up swaps the entry with its higher-ranked neighbor."""
        _ = admin_logged_in
        assert big_board_source.id is not None

        create_resp = await app_client.post(
            "/admin/boards",
            data={
                "news_source_id": str(big_board_source.id),
                "draft_year": "2026",
                "published_at": "2026-05-01",
            },
            follow_redirects=False,
        )
        board_id = int(
            create_resp.headers["location"].split("/")[-1].split("?")[0]
        )
        for i, player in enumerate(sample_players, start=1):
            await app_client.post(
                f"/admin/boards/{board_id}/entries",
                data={"player_id": str(player.id), "position": str(i)},
                follow_redirects=False,
            )

        rows = (
            await db_session.execute(
                select(BoardEntry.id, BoardEntry.position)  # type: ignore[call-overload]
                .where(BoardEntry.board_id == board_id)  # type: ignore[arg-type]
                .order_by(BoardEntry.position)  # type: ignore[arg-type]
            )
        ).all()
        # rows is [(id_at_rank_1, 1), (id_at_rank_2, 2), (id_at_rank_3, 3)]
        rank2_entry_id = rows[1].id
        rank1_entry_id = rows[0].id

        # Move rank-2 up -> should become rank 1
        up_resp = await app_client.post(
            f"/admin/boards/{board_id}/entries/{rank2_entry_id}/move-up",
            follow_redirects=False,
        )
        assert up_resp.status_code == 303

        rows_after = (
            await db_session.execute(
                select(BoardEntry.id, BoardEntry.position)  # type: ignore[call-overload]
                .where(BoardEntry.board_id == board_id)  # type: ignore[arg-type]
                .order_by(BoardEntry.position)  # type: ignore[arg-type]
            )
        ).all()
        assert rows_after[0].id == rank2_entry_id
        assert rows_after[1].id == rank1_entry_id

    async def test_delete_pending_board_removes_row(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_logged_in: None,
        big_board_source: NewsSource,
    ):
        """Hard-delete on a PENDING board removes it entirely."""
        _ = admin_logged_in
        assert big_board_source.id is not None

        create_resp = await app_client.post(
            "/admin/boards",
            data={
                "news_source_id": str(big_board_source.id),
                "draft_year": "2026",
                "published_at": "2026-05-01",
            },
            follow_redirects=False,
        )
        board_id = int(
            create_resp.headers["location"].split("/")[-1].split("?")[0]
        )

        del_resp = await app_client.post(
            f"/admin/boards/{board_id}/delete", follow_redirects=False
        )
        assert del_resp.status_code == 303
        assert del_resp.headers["location"].startswith("/admin/boards")

        assert await db_session.get(Board, board_id) is None


@pytest.mark.asyncio
class TestBigBoardListing:
    """List endpoint filters and renders without crashing."""

    async def test_list_renders_with_status_filter(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_logged_in: None,
        big_board_source: NewsSource,
        sample_players: list[PlayerMaster],
    ):
        """A PENDING board appears on the unfiltered list AND on ?status=PENDING."""
        _ = admin_logged_in
        assert big_board_source.id is not None

        # Seed one board directly so we don't depend on the create flow here
        published = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
            days=1
        )
        board = Board(
            news_source_id=big_board_source.id,
            draft_year=2026,
            published_at=published,
            size=1,
            status=BoardStatus.PENDING,
        )
        db_session.add(board)
        await db_session.flush()
        db_session.add(
            BoardEntry(
                board_id=board.id,
                player_id=sample_players[0].id,
                position=1,
            )
        )
        await db_session.commit()

        all_resp = await app_client.get("/admin/boards")
        assert all_resp.status_code == 200
        assert "Big Board Test Source" in all_resp.text

        pending_resp = await app_client.get("/admin/boards?status=PENDING")
        assert pending_resp.status_code == 200
        assert "Big Board Test Source" in pending_resp.text
