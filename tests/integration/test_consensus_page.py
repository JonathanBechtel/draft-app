"""Integration tests for the dedicated /consensus page (ticket #271).

Covers:
- GET /consensus returns 200 and renders the page shell.
- Heading reflects the calendar-determined kind (BIG_BOARD before lottery).
- Nav link "Consensus" is present on the page.
- Homepage hero contains the "View full board" link pointing to /consensus.
- No-snapshot (empty DB) renders an empty state without a 500.
- Route context includes all required section keys (board, sources, overlays,
  matrix, trajectories, movers, controversial, spotlight, freshness).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.boards import Board, BoardEntry, BoardKind, BoardStatus
from app.schemas.consensus import ConsensusTrigger
from app.schemas.news_sources import FeedType, NewsSource
from app.schemas.players_master import PlayerMaster
from app.services import consensus_service as svc
from tests.integration.conftest import make_player


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _make_source(db: AsyncSession, name: str) -> NewsSource:
    """Create and flush a NewsSource."""
    src = NewsSource(
        name=name,
        display_name=f"{name.title()} Display",
        feed_type=FeedType.RSS,
        feed_url=f"https://example.com/{name}/feed.xml",
        is_active=True,
        fetch_interval_minutes=30,
    )
    db.add(src)
    await db.flush()
    return src


async def _make_approved_board(
    db: AsyncSession,
    *,
    source: NewsSource,
    draft_year: int,
    entries: list[tuple[PlayerMaster, int]],
) -> Board:
    """Insert an APPROVED board without triggering hooks."""
    assert source.id is not None
    board = Board(
        news_source_id=source.id,
        news_item_id=None,
        draft_year=draft_year,
        published_at=_now() - timedelta(hours=24),
        size=len(entries),
        status=BoardStatus.APPROVED,
        approved_at=_now(),
    )
    db.add(board)
    await db.flush()
    assert board.id is not None
    for player, rank in entries:
        assert player.id is not None
        db.add(BoardEntry(board_id=board.id, player_id=player.id, position=rank))
    await db.flush()
    return board


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def seeded_consensus(db_session: AsyncSession) -> dict:
    """Seed three players, two sources, and a recomputed consensus snapshot.

    Returns a dict with the seeded players for assertion use.
    """
    p1 = make_player("Cooper", "Flagg", school="Duke")
    p2 = make_player("Dylan", "Harper", school="Rutgers")
    p3 = make_player("Ace", "Bailey", school="Kansas")
    for p in (p1, p2, p3):
        db_session.add(p)
    await db_session.flush()

    s1 = await _make_source(db_session, "cp-source-alpha")
    s2 = await _make_source(db_session, "cp-source-beta")

    entries = [(p1, 1), (p2, 2), (p3, 3)]
    await _make_approved_board(
        db_session, source=s1, draft_year=2026, entries=entries
    )
    await _make_approved_board(
        db_session, source=s2, draft_year=2026, entries=entries
    )
    await db_session.commit()

    await svc.recompute_consensus(
        db_session, draft_year=2026, trigger=ConsensusTrigger.MANUAL
    )
    await db_session.commit()

    return {"players": [p1, p2, p3], "sources": [s1, s2]}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consensus_page_returns_200(
    app_client: AsyncClient,
    seeded_consensus: dict,
) -> None:
    """GET /consensus with data returns 200 and renders the page shell.

    Asserts the response is 200, and the page heading and section placeholders
    are present in the HTML.
    """
    with patch(
        "app.routes.ui.get_consensus_board_kind",
        return_value=BoardKind.BIG_BOARD,
    ):
        resp = await app_client.get("/consensus")

    assert resp.status_code == 200
    html = resp.text
    # Page heading
    assert "Consensus" in html
    # Section containers
    assert "consensusBoardSection" in html
    assert "consensusScatterSection" in html
    assert "consensusSourcesSection" in html
    assert "consensusMatrixSection" in html
    assert "consensusTrajectoriesSection" in html
    assert "consensusPanelsSection" in html


@pytest.mark.asyncio
async def test_consensus_page_heading_big_board(
    app_client: AsyncClient,
    seeded_consensus: dict,
) -> None:
    """GET /consensus with BIG_BOARD kind shows 'Big Board' in the heading.

    The heading is calendar-determined via get_consensus_board_kind();
    when big-board rows exist the heading is forced to BIG_BOARD.
    """
    with patch(
        "app.routes.ui.get_consensus_board_kind",
        return_value=BoardKind.BIG_BOARD,
    ):
        resp = await app_client.get("/consensus")

    assert resp.status_code == 200
    html = resp.text
    assert "Big Board" in html


@pytest.mark.asyncio
async def test_consensus_page_no_snapshot_empty_state(
    app_client: AsyncClient,
) -> None:
    """GET /consensus with no snapshot data returns 200 with an empty state.

    When no consensus snapshot exists for the draft year the page must
    render gracefully — no 500 error, no unhandled exception.
    """
    resp = await app_client.get("/consensus")
    assert resp.status_code == 200
    # The page structure must still be present even with empty data.
    html = resp.text
    assert "consensusBoardSection" in html


@pytest.mark.asyncio
async def test_consensus_nav_link_present(
    app_client: AsyncClient,
) -> None:
    """The navbar links to the Draft hub on any page.

    Checks the consensus page itself since it includes navbar.html. The direct
    "Consensus Mock" nav link was consolidated into the /draft hub, which in turn
    links to the consensus board.
    """
    resp = await app_client.get("/consensus")
    assert resp.status_code == 200
    html = resp.text
    assert 'href="/draft"' in html
    assert "Consensus" in html


@pytest.mark.asyncio
async def test_homepage_hero_view_full_board_link(
    app_client: AsyncClient,
    seeded_consensus: dict,
) -> None:
    """Homepage consensus hero contains a 'View full board' link to /consensus.

    Confirms the CTA link added to home.html is present and points to /consensus.
    """
    with patch(
        "app.routes.ui.get_consensus_board_kind",
        return_value=BoardKind.BIG_BOARD,
    ):
        resp = await app_client.get("/")

    assert resp.status_code == 200
    html = resp.text
    assert 'href="/consensus"' in html
    assert "View full board" in html


@pytest.mark.asyncio
async def test_consensus_page_context_keys_populated(
    app_client: AsyncClient,
    seeded_consensus: dict,
) -> None:
    """GET /consensus with data renders all section placeholder containers.

    Each section partial includes a data-section attribute so we can verify
    the placeholder was included (i.e. all includes resolved without error).
    The actual content of each placeholder is filled by downstream tickets.
    """
    with patch(
        "app.routes.ui.get_consensus_board_kind",
        return_value=BoardKind.BIG_BOARD,
    ):
        resp = await app_client.get("/consensus")

    assert resp.status_code == 200
    html = resp.text

    # Each section partial must render its placeholder element.
    for section in ("board", "scatter", "sources", "matrix", "trajectories", "panels"):
        assert f'data-section="{section}"' in html, (
            f"Section placeholder for '{section}' not found in rendered HTML"
        )
