"""Integration tests for the admin extract-board trigger route.

POST /admin/news-items/{item_id}/extract-board triggers AI board extraction
and redirects to the resulting board detail page.

All network + Gemini calls are patched out so these tests do not hit any
real external service.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import httpx
import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.boards import Board, BoardKind, BoardStatus
from app.schemas.news_items import NewsItem, NewsItemTag
from app.schemas.news_sources import FeedType, NewsSource
from app.schemas.players_master import PlayerMaster
from app.services import board_extraction_service
from app.services.board_extraction_service import (
    BoardExtractionError,
    ExtractedBoard,
    ExtractedBoardEntry,
    PaywallDetectedError,
)
from tests.integration.auth_helpers import create_auth_user, login_staff


ADMIN_EMAIL = "extract-board-admin@example.com"
ADMIN_PASSWORD = "extract-board-password-123"


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def admin_logged_in(app_client: AsyncClient, db_session: AsyncSession) -> None:
    """Create an admin user and set the session cookie on the HTTP client."""
    await create_auth_user(
        db_session,
        email=ADMIN_EMAIL,
        role="admin",
        password=ADMIN_PASSWORD,
    )
    await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)


@pytest_asyncio.fixture
async def news_source(db_session: AsyncSession) -> NewsSource:
    """A basic active news source for test articles."""
    source = NewsSource(
        name="extract-trigger-source",
        display_name="Extract Trigger Test Source",
        feed_type=FeedType.RSS,
        feed_url="https://example.com/extract-trigger-feed.xml",
        is_active=True,
        fetch_interval_minutes=30,
    )
    db_session.add(source)
    await db_session.commit()
    await db_session.refresh(source)
    assert source.id is not None
    return source


@pytest_asyncio.fixture
async def big_board_item(db_session: AsyncSession, news_source: NewsSource) -> NewsItem:
    """A BIG_BOARD-tagged news item suitable for extraction."""
    assert news_source.id is not None
    item = NewsItem(
        source_id=news_source.id,
        external_id="trigger-test-bb-1",
        title="2026 NBA Draft Big Board",
        description="Ranking the top 30 prospects.",
        url="https://example.substack.com/p/2026-big-board-trigger-test",
        tag=NewsItemTag.BIG_BOARD,
        published_at=datetime(2026, 3, 15),
    )
    db_session.add(item)
    await db_session.commit()
    await db_session.refresh(item)
    assert item.id is not None
    return item


@pytest_asyncio.fixture
async def sample_player(db_session: AsyncSession) -> PlayerMaster:
    """A player whose name the extraction stub will resolve.

    Patches ``_schedule_player_embedding`` so committing this row does not
    fire a real Gemini embedding call against the production database.
    """
    with patch(
        "app.schemas.players_master._schedule_player_embedding",
        return_value=None,
    ):
        player = PlayerMaster(display_name="Cooper Flagg")
        db_session.add(player)
        await db_session.commit()
        await db_session.refresh(player)
    assert player.id is not None
    return player


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stub_extraction(monkeypatch, extracted: ExtractedBoard) -> None:
    """Replace _extract_via_gemini with a fixed-output stub."""

    async def _stub(article_text: str, *, client=None) -> ExtractedBoard:
        return extracted

    monkeypatch.setattr(board_extraction_service, "_extract_via_gemini", _stub)


def _stub_fetcher(monkeypatch, text: str = "article content") -> None:
    """Patch _default_fetcher so no real HTTP fetch occurs."""

    async def _fake_fetcher(url: str) -> str:
        return text

    # extract_board picks up the fetcher kwarg; we patch the module-level
    # default so route code that calls extract_board(...) without a fetcher kwarg
    # also gets our stub.
    monkeypatch.setattr(board_extraction_service, "_default_fetcher", _fake_fetcher)


def _stub_vector_search(monkeypatch) -> None:
    """Patch find_candidate_players so no real embedding calls occur."""

    async def _stub(db, query: str, k: int = 5):
        return []

    monkeypatch.setattr(board_extraction_service, "find_candidate_players", _stub)


def _stub_paywall(monkeypatch) -> None:
    """Make _default_fetcher raise PaywallDetectedError."""

    async def _fake_fetcher(url: str) -> str:
        raise PaywallDetectedError("Article is paywalled per test stub.")

    monkeypatch.setattr(board_extraction_service, "_default_fetcher", _fake_fetcher)


def _stub_extraction_error(monkeypatch, message: str) -> None:
    """Make _extract_via_gemini raise BoardExtractionError."""

    async def _stub(article_text: str, *, client=None) -> ExtractedBoard:
        raise BoardExtractionError(message)

    monkeypatch.setattr(board_extraction_service, "_extract_via_gemini", _stub)


def _stub_fetch_http_error(monkeypatch) -> None:
    """Make _default_fetcher raise a raw httpx error (e.g. dead URL/timeout)."""

    async def _fake_fetcher(url: str) -> str:
        raise httpx.ConnectError("simulated DNS/connect failure")

    monkeypatch.setattr(board_extraction_service, "_default_fetcher", _fake_fetcher)


# ---------------------------------------------------------------------------
# Auth guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_board_requires_login(
    app_client: AsyncClient,
    big_board_item: NewsItem,
) -> None:
    """Unauthenticated POST redirects to the admin login page.

    Verifies the login guard fires before any extraction logic.
    """
    assert big_board_item.id is not None
    response = await app_client.post(
        f"/admin/news-items/{big_board_item.id}/extract-board",
        follow_redirects=False,
    )
    assert response.status_code in {302, 303}
    assert "/admin/login" in response.headers.get("location", "")


# ---------------------------------------------------------------------------
# Happy path: new board created
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_board_creates_pending_board_and_redirects(
    app_client: AsyncClient,
    db_session: AsyncSession,
    admin_logged_in: None,
    big_board_item: NewsItem,
    sample_player: PlayerMaster,
    monkeypatch,
) -> None:
    """POST trigger creates a PENDING board and redirects to its detail page.

    The redirect URL carries ``success=extracted`` so the board detail page
    can surface the appropriate flash message.
    """
    _stub_fetcher(monkeypatch)
    _stub_vector_search(monkeypatch)
    _stub_extraction(
        monkeypatch,
        ExtractedBoard(
            draft_year=2026,
            published_at=datetime(2026, 3, 15),
            entries=[
                ExtractedBoardEntry(player_name="Cooper Flagg", rank=1),
            ],
        ),
    )

    assert big_board_item.id is not None
    response = await app_client.post(
        f"/admin/news-items/{big_board_item.id}/extract-board",
        follow_redirects=False,
    )

    assert response.status_code in {302, 303}
    location = response.headers.get("location", "")
    assert "/admin/boards/" in location
    assert "success=extracted" in location

    # Confirm a PENDING board was actually created in the DB.
    result = await db_session.execute(
        select(Board).where(Board.news_item_id == big_board_item.id)  # type: ignore[arg-type]
    )
    board = result.scalar_one_or_none()
    assert board is not None
    assert board.status == BoardStatus.PENDING
    assert board.kind == BoardKind.BIG_BOARD


# ---------------------------------------------------------------------------
# Duplicate: board already exists
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_board_duplicate_redirects_to_existing_board(
    app_client: AsyncClient,
    db_session: AsyncSession,
    admin_logged_in: None,
    big_board_item: NewsItem,
    sample_player: PlayerMaster,
    monkeypatch,
) -> None:
    """Triggering extraction on an item with an existing board avoids duplicates.

    The admin is redirected to the existing board with a
    ``success=already_extracted`` notice.
    """
    _stub_fetcher(monkeypatch)
    _stub_vector_search(monkeypatch)
    _stub_extraction(
        monkeypatch,
        ExtractedBoard(
            draft_year=2026,
            entries=[ExtractedBoardEntry(player_name="Cooper Flagg", rank=1)],
        ),
    )

    assert big_board_item.id is not None

    # First trigger — creates the board.
    first = await app_client.post(
        f"/admin/news-items/{big_board_item.id}/extract-board",
        follow_redirects=False,
    )
    assert first.status_code in {302, 303}
    first_location = first.headers.get("location", "")
    assert "success=extracted" in first_location

    # Extract the board id from the first redirect.
    import re

    board_id_match = re.search(r"/admin/boards/(\d+)", first_location)
    assert board_id_match is not None
    first_board_id = int(board_id_match.group(1))

    # Second trigger — should return the existing board.
    second = await app_client.post(
        f"/admin/news-items/{big_board_item.id}/extract-board",
        follow_redirects=False,
    )
    assert second.status_code in {302, 303}
    second_location = second.headers.get("location", "")
    assert f"/admin/boards/{first_board_id}" in second_location
    assert "success=already_extracted" in second_location

    # Only one Board row in the DB.
    result = await db_session.execute(
        select(Board).where(Board.news_item_id == big_board_item.id)  # type: ignore[arg-type]
    )
    boards = result.scalars().all()
    assert len(boards) == 1


# ---------------------------------------------------------------------------
# Paywall error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_board_paywalled_redirects_back_with_error(
    app_client: AsyncClient,
    admin_logged_in: None,
    big_board_item: NewsItem,
    monkeypatch,
) -> None:
    """A paywalled article redirects back to the news item with an error.

    The error message in the redirect URL contains paywall-related text.
    """
    _stub_paywall(monkeypatch)

    assert big_board_item.id is not None
    response = await app_client.post(
        f"/admin/news-items/{big_board_item.id}/extract-board",
        follow_redirects=False,
    )

    assert response.status_code in {302, 303}
    location = response.headers.get("location", "")
    assert f"/admin/news-items/{big_board_item.id}" in location
    assert "error=" in location
    # The URL-encoded error message should contain paywall-related text.
    assert "paywall" in location.lower() or "paywalled" in location.lower()


@pytest.mark.asyncio
async def test_extract_board_fetch_failure_redirects_with_error(
    app_client: AsyncClient,
    admin_logged_in: None,
    big_board_item: NewsItem,
    monkeypatch,
) -> None:
    """A dead article URL (raw httpx error) redirects back, not a 500.

    Defends the route's promise to surface fetch failures cleanly even if a
    transport error reaches it unwrapped.
    """
    _stub_fetch_http_error(monkeypatch)

    assert big_board_item.id is not None
    response = await app_client.post(
        f"/admin/news-items/{big_board_item.id}/extract-board",
        follow_redirects=False,
    )

    assert response.status_code in {302, 303}
    location = response.headers.get("location", "")
    assert f"/admin/news-items/{big_board_item.id}" in location
    assert "error=" in location


# ---------------------------------------------------------------------------
# No entries extracted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_board_no_entries_redirects_back_with_notice(
    app_client: AsyncClient,
    admin_logged_in: None,
    big_board_item: NewsItem,
    monkeypatch,
) -> None:
    """No ranked entries from AI redirects back with a clear notice.

    When the AI returns zero entries the admin is redirected back to the
    news item page — no board is created.
    """
    _stub_fetcher(monkeypatch)
    _stub_extraction(
        monkeypatch,
        ExtractedBoard(draft_year=2026, entries=[]),
    )

    assert big_board_item.id is not None
    response = await app_client.post(
        f"/admin/news-items/{big_board_item.id}/extract-board",
        follow_redirects=False,
    )

    assert response.status_code in {302, 303}
    location = response.headers.get("location", "")
    assert f"/admin/news-items/{big_board_item.id}" in location
    assert "error=" in location


# ---------------------------------------------------------------------------
# Generic BoardExtractionError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_board_generic_error_redirects_back(
    app_client: AsyncClient,
    admin_logged_in: None,
    big_board_item: NewsItem,
    monkeypatch,
) -> None:
    """A BoardExtractionError is surfaced as an error redirect, not a 500.

    When the AI step raises (e.g., malformed response) the admin is sent back
    to the news item page with an informative error query parameter.
    """
    _stub_fetcher(monkeypatch)
    _stub_extraction_error(monkeypatch, "Gemini returned an empty response.")

    assert big_board_item.id is not None
    response = await app_client.post(
        f"/admin/news-items/{big_board_item.id}/extract-board",
        follow_redirects=False,
    )

    assert response.status_code in {302, 303}
    location = response.headers.get("location", "")
    assert f"/admin/news-items/{big_board_item.id}" in location
    assert "error=" in location
