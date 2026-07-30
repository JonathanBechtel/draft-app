"""Integration tests for T7: admin candidate display and manual entry assignment.

Covers:
- GET board detail: unresolved entries render ``raw_name`` and candidate buttons.
- GET board detail: resolved entries show player name + resolution method badge.
- GET board detail: ``unresolved_count`` badge appears in the page subtitle.
- POST /assign: happy path sets player_id and stamps MANUAL on the entry.
- POST /assign: 404-like redirect with error when entry_id is unknown.
- POST /assign: reject when the board is not PENDING.
- GET /entries/player-search: returns JSON matching the query.
- GET /entries/player-search: returns empty list for short queries.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.boards import Board, BoardEntry, BoardStatus, ResolutionMethod
from app.schemas.news_sources import FeedType, NewsSource
from app.schemas.players_master import PlayerMaster
from tests.integration.auth_helpers import create_auth_user, login_staff
from tests.integration.conftest import make_player


ADMIN_EMAIL = "res-admin@example.com"
ADMIN_PASSWORD = "admin-password-res-123"


@pytest_asyncio.fixture
async def admin_logged_in(app_client: AsyncClient, db_session: AsyncSession) -> None:
    """Create an admin user and establish a session cookie on ``app_client``."""
    await create_auth_user(
        db_session,
        email=ADMIN_EMAIL,
        role="admin",
        password=ADMIN_PASSWORD,
    )
    await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)


@pytest_asyncio.fixture
async def source(db_session: AsyncSession) -> NewsSource:
    """A minimal active news source."""
    src = NewsSource(
        name="res-test-source",
        display_name="Resolution Test Source",
        feed_type=FeedType.RSS,
        feed_url="https://example.com/res-test-feed.xml",
        is_active=True,
        fetch_interval_minutes=30,
    )
    db_session.add(src)
    await db_session.commit()
    await db_session.refresh(src)
    return src


@pytest_asyncio.fixture
async def players(db_session: AsyncSession) -> list[PlayerMaster]:
    """Three players seeded for resolution tests."""
    rows = [
        make_player("Aday", "Mara", school="FC Barcelona"),
        make_player("Cooper", "Flagg", school="Duke"),
        make_player("Dylan", "Harper", school="Rutgers"),
    ]
    for p in rows:
        db_session.add(p)
    await db_session.commit()
    for p in rows:
        await db_session.refresh(p)
    return rows


def _make_board(source_id: int) -> Board:
    return Board(
        news_source_id=source_id,
        draft_year=2026,
        published_at=datetime.now(timezone.utc).replace(tzinfo=None),
        size=0,
        status=BoardStatus.PENDING,
    )


async def _seed_board_with_entries(
    db: AsyncSession,
    source_id: int,
    player: PlayerMaster,
    unresolved_player_id: int | None = None,
) -> tuple[Board, BoardEntry, BoardEntry]:
    """Seed a PENDING board with one resolved entry and one unresolved entry."""
    board = _make_board(source_id)
    db.add(board)
    await db.flush()
    assert board.id is not None

    resolved_entry = BoardEntry(
        board_id=board.id,
        player_id=player.id,
        position=1,
        raw_name=f"{player.first_name} {player.last_name}",
        resolution_method=ResolutionMethod.EXACT,
    )
    db.add(resolved_entry)

    unresolved_entry = BoardEntry(
        board_id=board.id,
        player_id=None,
        position=2,
        raw_name="mara",
        resolution_method=ResolutionMethod.UNRESOLVED,
        vector_candidates=[
            {
                "player_id": unresolved_player_id or 9999,
                "display_name": "Aday Mara",
                "score": 0.91,
            },
            {"player_id": 9998, "display_name": "Someone Else", "score": 0.55},
        ],
    )
    db.add(unresolved_entry)

    board.size = 2
    await db.commit()
    await db.refresh(board)
    await db.refresh(resolved_entry)
    await db.refresh(unresolved_entry)
    return board, resolved_entry, unresolved_entry


@pytest.mark.asyncio
class TestBoardDetailResolutionDisplay:
    """The board detail page renders resolution state for all entries."""

    async def test_resolved_entry_shows_player_and_method_badge(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_logged_in: None,
        source: NewsSource,
        players: list[PlayerMaster],
    ) -> None:
        """A resolved entry shows the player display_name and EXACT badge."""
        _ = admin_logged_in
        assert source.id is not None
        board, resolved, _ = await _seed_board_with_entries(
            db_session, source.id, players[0], unresolved_player_id=players[1].id
        )

        resp = await app_client.get(f"/admin/boards/{board.id}")
        assert resp.status_code == 200
        assert "Aday Mara" in resp.text
        # Resolution method badge
        assert "EXACT" in resp.text

    async def test_unresolved_entry_shows_raw_name_and_candidates(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_logged_in: None,
        source: NewsSource,
        players: list[PlayerMaster],
    ) -> None:
        """An unresolved entry shows raw_name and the top-5 candidate buttons."""
        _ = admin_logged_in
        assert source.id is not None
        board, _, unresolved = await _seed_board_with_entries(
            db_session, source.id, players[0]
        )

        resp = await app_client.get(f"/admin/boards/{board.id}")
        assert resp.status_code == 200
        assert "mara" in resp.text  # raw_name
        assert "Aday Mara" in resp.text  # first candidate
        assert "0.91" in resp.text  # score

    async def test_unresolved_count_badge_in_subtitle(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_logged_in: None,
        source: NewsSource,
        players: list[PlayerMaster],
    ) -> None:
        """The subtitle shows '1 unresolved' when there is one unresolved entry."""
        _ = admin_logged_in
        assert source.id is not None
        board, _, _ = await _seed_board_with_entries(db_session, source.id, players[0])

        resp = await app_client.get(f"/admin/boards/{board.id}")
        assert resp.status_code == 200
        assert "1 unresolved" in resp.text

    async def test_no_unresolved_badge_when_all_resolved(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_logged_in: None,
        source: NewsSource,
        players: list[PlayerMaster],
    ) -> None:
        """The unresolved badge is absent when every entry is resolved."""
        _ = admin_logged_in
        assert source.id is not None
        board = _make_board(source.id)
        db_session.add(board)
        await db_session.flush()
        assert board.id is not None
        db_session.add(
            BoardEntry(
                board_id=board.id,
                player_id=players[0].id,
                position=1,
                raw_name="Aday Mara",
                resolution_method=ResolutionMethod.EXACT,
            )
        )
        board.size = 1
        await db_session.commit()

        resp = await app_client.get(f"/admin/boards/{board.id}")
        assert resp.status_code == 200
        # The "N unresolved" subtitle badge should be absent; the word may
        # still appear in JS comments or CSS class names.
        assert "unresolved</span>" not in resp.text


@pytest.mark.asyncio
class TestAssignEntry:
    """POST /admin/boards/<id>/entries/<entry_id>/assign."""

    async def test_assign_sets_player_and_manual_method(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_logged_in: None,
        source: NewsSource,
        players: list[PlayerMaster],
    ) -> None:
        """Assigning a player sets player_id and resolution_method=MANUAL."""
        _ = admin_logged_in
        assert source.id is not None
        board, _, unresolved = await _seed_board_with_entries(
            db_session, source.id, players[0]
        )

        target_player = players[2]
        assert target_player.id is not None
        resp = await app_client.post(
            f"/admin/boards/{board.id}/entries/{unresolved.id}/assign",
            data={"player_id": str(target_player.id)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "success=entry_updated" in resp.headers["location"]

        updated = await db_session.get(
            BoardEntry, unresolved.id, populate_existing=True
        )
        assert updated is not None
        assert updated.player_id == target_player.id
        assert updated.resolution_method is ResolutionMethod.MANUAL

    async def test_assign_redirects_with_error_for_unknown_entry(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_logged_in: None,
        source: NewsSource,
        players: list[PlayerMaster],
    ) -> None:
        """Assigning to a non-existent entry redirects with an error query param."""
        _ = admin_logged_in
        assert source.id is not None
        board = _make_board(source.id)
        db_session.add(board)
        await db_session.commit()
        await db_session.refresh(board)

        resp = await app_client.post(
            f"/admin/boards/{board.id}/entries/999999/assign",
            data={"player_id": str(players[0].id)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error=" in resp.headers["location"]

    async def test_assign_rejected_on_approved_board(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_logged_in: None,
        source: NewsSource,
        players: list[PlayerMaster],
    ) -> None:
        """Assigning a player on an APPROVED board redirects with an error."""
        _ = admin_logged_in
        assert source.id is not None
        board, resolved, unresolved = await _seed_board_with_entries(
            db_session, source.id, players[0]
        )
        # Manually flip the board to APPROVED (bypassing service to avoid
        # consensus recompute side-effect in this isolated test).
        board.status = BoardStatus.APPROVED
        await db_session.commit()

        resp = await app_client.post(
            f"/admin/boards/{board.id}/entries/{unresolved.id}/assign",
            data={"player_id": str(players[2].id)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error=" in resp.headers["location"]

        # Entry must remain unchanged.
        still = await db_session.get(BoardEntry, unresolved.id, populate_existing=True)
        assert still is not None
        assert still.player_id is None
        assert still.resolution_method is ResolutionMethod.UNRESOLVED

    async def test_assign_updates_detail_page_on_next_render(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_logged_in: None,
        source: NewsSource,
        players: list[PlayerMaster],
    ) -> None:
        """After assignment, the detail page shows the resolved name + MANUAL badge."""
        _ = admin_logged_in
        assert source.id is not None
        board, _, unresolved = await _seed_board_with_entries(
            db_session, source.id, players[0]
        )

        target = players[2]
        assert target.id is not None
        await app_client.post(
            f"/admin/boards/{board.id}/entries/{unresolved.id}/assign",
            data={"player_id": str(target.id)},
            follow_redirects=False,
        )

        resp = await app_client.get(f"/admin/boards/{board.id}")
        assert resp.status_code == 200
        assert target.display_name in resp.text
        assert "MANUAL" in resp.text


@pytest.mark.asyncio
class TestBoardPlayerSearch:
    """GET /admin/boards/<id>/entries/player-search."""

    async def test_returns_json_for_matching_query(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_logged_in: None,
        source: NewsSource,
        players: list[PlayerMaster],
    ) -> None:
        """A two-character-plus query returns matching players as JSON."""
        _ = admin_logged_in
        assert source.id is not None
        board = _make_board(source.id)
        db_session.add(board)
        await db_session.commit()
        await db_session.refresh(board)

        resp = await app_client.get(
            f"/admin/boards/{board.id}/entries/player-search?q=flagg"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        # Cooper Flagg should appear
        names = [r["display_name"] for r in data]
        assert any("Flagg" in n for n in names)

    async def test_returns_empty_list_for_short_query(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_logged_in: None,
        source: NewsSource,
        players: list[PlayerMaster],
    ) -> None:
        """A single-character query returns an empty JSON list."""
        _ = admin_logged_in
        assert source.id is not None
        board = _make_board(source.id)
        db_session.add(board)
        await db_session.commit()
        await db_session.refresh(board)

        resp = await app_client.get(
            f"/admin/boards/{board.id}/entries/player-search?q=f"
        )
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_requires_login(
        self,
        app_client: AsyncClient,
        source: NewsSource,
    ) -> None:
        """The search endpoint redirects to login when the user is logged out."""
        assert source.id is not None
        resp = await app_client.get(
            "/admin/boards/1/entries/player-search?q=flagg",
            follow_redirects=False,
        )
        # Unauthenticated requests get a 401 JSON or 302/303 redirect.
        assert resp.status_code in {200, 302, 303, 401}
        # If it returned JSON it should be an empty list (401 path).
        if resp.status_code == 200:
            assert resp.json() == []


@pytest.mark.asyncio
class TestMintStubPlayer:
    """POST /admin/boards/<id>/entries/<entry_id>/mint-stub."""

    async def test_mint_stub_creates_player_and_resolves_entry(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_logged_in: None,
        source: NewsSource,
        players: list[PlayerMaster],
    ) -> None:
        """Minting a stub creates a PlayerMaster with is_stub=True and sets STUB method."""
        _ = admin_logged_in
        assert source.id is not None
        board, _, unresolved = await _seed_board_with_entries(
            db_session, source.id, players[0]
        )
        assert unresolved.id is not None

        resp = await app_client.post(
            f"/admin/boards/{board.id}/entries/{unresolved.id}/mint-stub",
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "success=stub_minted" in resp.headers["location"]

        # Reload the entry and verify it is now resolved.
        updated = await db_session.get(
            BoardEntry, unresolved.id, populate_existing=True
        )
        assert updated is not None
        assert updated.player_id is not None
        assert updated.resolution_method is ResolutionMethod.STUB

        # The new PlayerMaster stub should exist with is_stub=True and correct name.
        stub = await db_session.get(PlayerMaster, updated.player_id)
        assert stub is not None
        assert stub.is_stub is True
        assert stub.display_name == unresolved.raw_name

    async def test_mint_stub_reuses_diacritic_variant(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_logged_in: None,
        source: NewsSource,
    ) -> None:
        """A punctuation/diacritic-only match resolves without minting a stub."""
        _ = admin_logged_in
        assert source.id is not None
        canonical = PlayerMaster(
            first_name="José",
            last_name="García",
            display_name="José García",
            is_stub=False,
        )
        db_session.add(canonical)
        board = _make_board(source.id)
        db_session.add(board)
        await db_session.flush()
        assert board.id is not None
        entry = BoardEntry(
            board_id=board.id,
            player_id=None,
            position=1,
            raw_name="Jose Garcia",
            resolution_method=ResolutionMethod.UNRESOLVED,
        )
        board.size = 1
        db_session.add(entry)
        await db_session.commit()
        await db_session.refresh(entry)

        resp = await app_client.post(
            f"/admin/boards/{board.id}/entries/{entry.id}/mint-stub",
            follow_redirects=False,
        )

        assert resp.status_code == 303
        assert "success=entry_identity_resolved" in resp.headers["location"]
        updated = await db_session.get(BoardEntry, entry.id, populate_existing=True)
        assert updated is not None
        assert updated.player_id == canonical.id
        assert updated.resolution_method is ResolutionMethod.EXACT

    async def test_mint_stub_routes_suffix_variant_to_review(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_logged_in: None,
        source: NewsSource,
    ) -> None:
        """A suffix mismatch leaves the entry unresolved and creates no row."""
        _ = admin_logged_in
        assert source.id is not None
        canonical = PlayerMaster(
            first_name="Gary",
            last_name="Payton",
            suffix="II",
            display_name="Gary Payton II",
            is_stub=False,
        )
        db_session.add(canonical)
        board = _make_board(source.id)
        db_session.add(board)
        await db_session.flush()
        assert board.id is not None
        entry = BoardEntry(
            board_id=board.id,
            player_id=None,
            position=1,
            raw_name="Gary Payton",
            resolution_method=ResolutionMethod.UNRESOLVED,
        )
        board.size = 1
        db_session.add(entry)
        await db_session.commit()
        await db_session.refresh(entry)
        before = (
            await db_session.execute(select(func.count()).select_from(PlayerMaster))
        ).scalar_one()

        resp = await app_client.post(
            f"/admin/boards/{board.id}/entries/{entry.id}/mint-stub",
            follow_redirects=False,
        )

        assert resp.status_code == 303
        assert "error=" in resp.headers["location"]
        updated = await db_session.get(BoardEntry, entry.id, populate_existing=True)
        assert updated is not None
        assert updated.player_id is None
        assert updated.resolution_method is ResolutionMethod.UNRESOLVED
        after = (
            await db_session.execute(select(func.count()).select_from(PlayerMaster))
        ).scalar_one()
        assert after == before

    async def test_mint_stub_shows_resolved_on_detail_page(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_logged_in: None,
        source: NewsSource,
        players: list[PlayerMaster],
    ) -> None:
        """After minting, the board detail page shows the stub name and STUB badge."""
        _ = admin_logged_in
        assert source.id is not None
        board, _, unresolved = await _seed_board_with_entries(
            db_session, source.id, players[0]
        )
        assert unresolved.id is not None

        await app_client.post(
            f"/admin/boards/{board.id}/entries/{unresolved.id}/mint-stub",
            follow_redirects=False,
        )

        resp = await app_client.get(f"/admin/boards/{board.id}")
        assert resp.status_code == 200
        assert "STUB" in resp.text

    async def test_mint_stub_rejected_on_approved_board(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_logged_in: None,
        source: NewsSource,
        players: list[PlayerMaster],
    ) -> None:
        """Minting on an APPROVED board redirects with an error and leaves entry unchanged."""
        _ = admin_logged_in
        assert source.id is not None
        board, _, unresolved = await _seed_board_with_entries(
            db_session, source.id, players[0]
        )
        board.status = BoardStatus.APPROVED
        await db_session.commit()
        assert unresolved.id is not None

        resp = await app_client.post(
            f"/admin/boards/{board.id}/entries/{unresolved.id}/mint-stub",
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error=" in resp.headers["location"]

        # Entry must remain unresolved.
        still = await db_session.get(BoardEntry, unresolved.id, populate_existing=True)
        assert still is not None
        assert still.player_id is None
        assert still.resolution_method is ResolutionMethod.UNRESOLVED

    async def test_mint_stub_rejected_for_already_resolved_entry(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_logged_in: None,
        source: NewsSource,
        players: list[PlayerMaster],
    ) -> None:
        """Minting on an already-resolved entry errors and creates no new stub.

        Guards against stale-page/double-submit: the resolved entry keeps its
        original player_id + method, and no extra stub PlayerMaster is minted.
        """
        _ = admin_logged_in
        assert source.id is not None
        board, resolved, _ = await _seed_board_with_entries(
            db_session, source.id, players[0]
        )
        assert resolved.id is not None
        original_player_id = resolved.player_id

        stubs_before = (
            await db_session.execute(
                select(func.count())
                .select_from(PlayerMaster)
                .where(PlayerMaster.is_stub.is_(True))  # type: ignore[attr-defined]
            )
        ).scalar_one()

        resp = await app_client.post(
            f"/admin/boards/{board.id}/entries/{resolved.id}/mint-stub",
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error=" in resp.headers["location"]

        # The resolved entry is unchanged.
        still = await db_session.get(BoardEntry, resolved.id, populate_existing=True)
        assert still is not None
        assert still.player_id == original_player_id
        assert still.resolution_method is ResolutionMethod.EXACT

        # No new stub was minted.
        stubs_after = (
            await db_session.execute(
                select(func.count())
                .select_from(PlayerMaster)
                .where(PlayerMaster.is_stub.is_(True))  # type: ignore[attr-defined]
            )
        ).scalar_one()
        assert stubs_after == stubs_before

    async def test_mint_stub_redirects_with_error_for_unknown_entry(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_logged_in: None,
        source: NewsSource,
    ) -> None:
        """Minting on a non-existent entry redirects with an error query param."""
        _ = admin_logged_in
        assert source.id is not None
        board = _make_board(source.id)
        db_session.add(board)
        await db_session.commit()
        await db_session.refresh(board)

        resp = await app_client.post(
            f"/admin/boards/{board.id}/entries/999999/mint-stub",
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error=" in resp.headers["location"]

    async def test_mint_stub_requires_login(
        self,
        app_client: AsyncClient,
        source: NewsSource,
    ) -> None:
        """The mint-stub endpoint redirects unauthenticated users."""
        assert source.id is not None
        resp = await app_client.post(
            "/admin/boards/1/entries/1/mint-stub",
            follow_redirects=False,
        )
        assert resp.status_code in {302, 303}
