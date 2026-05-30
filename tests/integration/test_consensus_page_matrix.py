"""Integration tests for the source breakdown matrix section on /consensus (ticket #275).

Covers:
- Matrix table renders player rows and source column headers with seeded data.
- Outlier cells carry the expected CSS classes (out-high / out-low).
- Source column headers contain anchor tags pointing to /sources/{slug}.
- Empty-state renders gracefully when no snapshot exists.
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
    """Create and flush a NewsSource with the given name."""
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
    """Insert an APPROVED board with the given source and player rankings."""
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
async def matrix_consensus(db_session: AsyncSession) -> dict:
    """Seed players and sources with deliberate outlier ranks for matrix testing.

    Source Alpha ranks all players conventionally (ranks 1-3).
    Source Beta diverges on player 2: ranks them at 5 (low outlier vs consensus 2).
    Source Beta also diverges on player 3: ranks them at 1 (high outlier vs consensus 3).

    Returns the seeded objects so tests can assert on player/source names.
    """
    p1 = make_player("Alice", "Allstar", school="Duke")
    p2 = make_player("Bob", "Baller", school="Kansas")
    p3 = make_player("Carl", "Court", school="UNC")
    for p in (p1, p2, p3):
        db_session.add(p)
    await db_session.flush()

    s1 = await _make_source(db_session, "Matrix Alpha")
    s2 = await _make_source(db_session, "Matrix Beta")

    # Source Alpha: conventional order
    await _make_approved_board(
        db_session,
        source=s1,
        draft_year=2026,
        entries=[(p1, 1), (p2, 2), (p3, 3)],
    )
    # Source Beta: p2 ranked 5 (low outlier), p3 ranked 1 (high outlier if threshold met)
    await _make_approved_board(
        db_session,
        source=s2,
        draft_year=2026,
        entries=[(p1, 1), (p2, 5), (p3, 1)],
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
async def test_breakdown_matrix_markup(
    app_client: AsyncClient,
    matrix_consensus: dict,
) -> None:
    """GET /consensus renders matrix table with player rows and source columns.

    Asserts that:
    - The matrix table element is present in the HTML.
    - Each seeded player name appears in a table row.
    - Each source name abbreviation appears in a column header link.
    - Source header links point to /sources/{slug}.
    - Outlier CSS classes are present when divergence exists.
    """
    with patch(
        "app.routes.ui.get_consensus_board_kind",
        return_value=BoardKind.BIG_BOARD,
    ):
        resp = await app_client.get("/consensus")

    assert resp.status_code == 200
    html = resp.text

    # Matrix section container is rendered (not the old placeholder).
    assert "matrix-section" in html
    assert "matrix-table" in html

    # Player names appear in the table.
    assert "Alice" in html
    assert "Bob" in html
    assert "Carl" in html

    # Source header links link to internal /sources/{slug}.
    assert 'href="/sources/' in html

    # Source header links carry the title attribute for the full source name.
    assert 'title="Matrix Alpha"' in html
    assert 'title="Matrix Beta"' in html

    # Outlier classes must appear (source beta diverges enough to trigger them).
    # The outlier threshold is defined in consensus_read_service; with p2 at rank 5
    # vs consensus 2 (delta +3) and p3 at rank 1 vs consensus 3 (delta -2) at least
    # one outlier class should appear if the threshold is ≤ 3.
    assert "out-high" in html or "out-low" in html


@pytest.mark.asyncio
async def test_breakdown_matrix_outlier_classes(
    app_client: AsyncClient,
    matrix_consensus: dict,
) -> None:
    """Matrix cells for large-delta source ranks carry the correct CSS class.

    Source Beta ranks p2 at position 5, consensus is 2 (delta = +3, low outlier)
    when the outlier threshold is < 3.  Source Beta ranks p3 at position 1,
    consensus is 3 (delta = -2, high outlier) when threshold is < 2.
    At least one of these must result in an outlier-styled cell.
    """
    with patch(
        "app.routes.ui.get_consensus_board_kind",
        return_value=BoardKind.BIG_BOARD,
    ):
        resp = await app_client.get("/consensus")

    assert resp.status_code == 200
    html = resp.text

    # At least one outlier cell class must be present.
    has_outlier = "matrix-table__td--out-high" in html or "matrix-table__td--out-low" in html
    assert has_outlier, (
        "Expected at least one outlier-highlighted cell in the matrix, but neither "
        "'.matrix-table__td--out-high' nor '.matrix-table__td--out-low' found in HTML."
    )


@pytest.mark.asyncio
async def test_breakdown_matrix_header_link_attrs(
    app_client: AsyncClient,
    matrix_consensus: dict,
) -> None:
    """Source column header links include href to /sources/{slug} and rel=noopener.

    Per the DoD, source headers must link out with appropriate security attributes.
    """
    with patch(
        "app.routes.ui.get_consensus_board_kind",
        return_value=BoardKind.BIG_BOARD,
    ):
        resp = await app_client.get("/consensus")

    assert resp.status_code == 200
    html = resp.text

    # Link href uses the /sources/ path with the source slug.
    assert 'href="/sources/matrix-alpha"' in html
    assert 'href="/sources/matrix-beta"' in html

    # rel=noopener is present for external-style link safety.
    assert 'rel="noopener"' in html


@pytest.mark.asyncio
async def test_breakdown_matrix_empty_state(
    app_client: AsyncClient,
) -> None:
    """GET /consensus with no snapshot data renders the empty matrix state gracefully.

    No 500 error; the empty variant of the matrix section should be shown.
    """
    resp = await app_client.get("/consensus")
    assert resp.status_code == 200
    html = resp.text

    # Even with no data, the matrix section container must render.
    assert "matrix-section" in html
    # The old placeholder div should be gone; instead the empty-state div renders.
    assert "consensus-matrix-placeholder" not in html
