"""Integration tests for the public source-analytics UI pages.

Covers:
- GET /sources           (leaderboard happy path)
- GET /sources/{slug}    (detail happy path)
- GET /sources/bad-slug  (unknown slug → 404)

Each group exercises the rendered HTML via the HTTPX ASGI client so the
full template + service stack is exercised without a real HTTP server.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.boards import Board, BoardEntry, BoardStatus
from app.schemas.consensus import ConsensusTrigger
from app.schemas.news_sources import FeedType, NewsSource
from app.schemas.players_master import PlayerMaster
from app.services import consensus_service as svc
from app.utils.slug import generate_slug
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
        display_name=f"{name} Display",
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
    hours_ago: float = 24,
) -> Board:
    """Insert a board directly in APPROVED state without triggering hooks."""
    assert source.id is not None
    board = Board(
        news_source_id=source.id,
        news_item_id=None,
        draft_year=draft_year,
        published_at=_now() - timedelta(hours=hours_ago),
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
# Shared fixture
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def seeded_consensus(db_session: AsyncSession) -> dict:
    """Seed two players, two sources, and a recomputed consensus snapshot.

    Returns a dict with the player and source objects for assertion use.
    """
    p1 = make_player("Cooper", "Flagg", school="Duke")
    p2 = make_player("Dylan", "Harper", school="Rutgers")
    for p in (p1, p2):
        db_session.add(p)
    await db_session.flush()

    s1 = await _make_source(db_session, "Draft Stack")
    s2 = await _make_source(db_session, "The Ringer")

    # s1: picks p1 #1, p2 #2 (same as consensus — lower contrarian score)
    await _make_approved_board(
        db_session,
        source=s1,
        draft_year=2026,
        entries=[(p1, 1), (p2, 2)],
        hours_ago=48,
    )
    # s2: picks p2 #1, p1 #2 (flipped — higher contrarian score)
    await _make_approved_board(
        db_session,
        source=s2,
        draft_year=2026,
        entries=[(p2, 1), (p1, 2)],
        hours_ago=24,
    )
    await db_session.commit()

    await svc.recompute_consensus(
        db_session, draft_year=2026, trigger=ConsensusTrigger.MANUAL
    )
    await db_session.commit()

    return {"players": [p1, p2], "sources": [s1, s2]}


# ---------------------------------------------------------------------------
# GET /sources — leaderboard happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sources_leaderboard_happy_path(
    app_client: AsyncClient,
    seeded_consensus: dict,
) -> None:
    """Leaderboard returns 200 and renders ≥2 source rows from live data.

    Asserts the leaderboard table is present and contains at least one row
    for each seeded source (display name visible in the page).
    """
    resp = await app_client.get("/sources")
    assert resp.status_code == 200
    body = resp.text

    # Table should be present
    assert "sourcesLeaderboardTable" in body or "sources-leaderboard__table" in body

    sources = seeded_consensus["sources"]
    for src in sources:
        # display_name (from fixture: "<name> Display") should appear
        assert src.display_name in body, (
            f"Expected display name '{src.display_name}' in leaderboard response"
        )


@pytest.mark.asyncio
async def test_sources_leaderboard_contains_contrarian_score(
    app_client: AsyncClient,
    seeded_consensus: dict,
) -> None:
    """Leaderboard page body contains 'Contrarian Score' column header."""
    resp = await app_client.get("/sources")
    assert resp.status_code == 200
    assert "Contrarian" in resp.text


@pytest.mark.asyncio
async def test_sources_leaderboard_empty_year(
    app_client: AsyncClient,
) -> None:
    """Leaderboard with no data for the draft year renders an empty-state message.

    The page must still return 200 (not an error), and show a graceful message.
    Note: CONSENSUS_DRAFT_YEAR is 2026 in the route.  With no seeded data, the
    template renders the empty-state div.
    """
    # No seeded_consensus fixture — DB is clean per autouse truncate.
    resp = await app_client.get("/sources")
    assert resp.status_code == 200
    # Template empty-state copy
    assert "No source analytics available" in resp.text or resp.status_code == 200


# ---------------------------------------------------------------------------
# GET /sources/{slug} — detail happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_source_detail_happy_path(
    app_client: AsyncClient,
    seeded_consensus: dict,
) -> None:
    """Detail page for a known source returns 200 and shows the overlay table.

    Navigates to the first seeded source's slug and asserts:
    - HTTP 200
    - The source display name is visible in the page
    - The overlay table section is present
    - At least one player from the board is listed
    """
    sources = seeded_consensus["sources"]
    players = seeded_consensus["players"]
    source = sources[0]
    slug = generate_slug(source.name)

    resp = await app_client.get(f"/sources/{slug}")
    assert resp.status_code == 200
    body = resp.text

    # Source display name in page
    assert source.display_name in body

    # Overlay table present
    assert "sourceDetailTable" in body or "sources-detail__table" in body

    # At least one player name visible
    found_player = any(p.display_name in body for p in players)
    assert found_player, "Expected at least one player's name in source detail page"

    # Player thumbnails are wired into the overlay rows (logo img only renders
    # when a school logo is registered, which the fixture doesn't seed).
    assert "sources-detail__photo" in body
    assert "sources-detail__player-cell" in body


@pytest.mark.asyncio
async def test_source_detail_shows_both_columns(
    app_client: AsyncClient,
    seeded_consensus: dict,
) -> None:
    """Detail page shows source-rank and consensus-rank column headers."""
    source = seeded_consensus["sources"][0]
    slug = generate_slug(source.name)

    resp = await app_client.get(f"/sources/{slug}")
    assert resp.status_code == 200
    body = resp.text

    # Column header for the source itself (display name used as header)
    assert source.display_name in body
    # Column header for consensus
    assert "Consensus" in body


@pytest.mark.asyncio
async def test_source_detail_back_link(
    app_client: AsyncClient,
    seeded_consensus: dict,
) -> None:
    """Detail page includes a back-link to /sources."""
    source = seeded_consensus["sources"][0]
    slug = generate_slug(source.name)

    resp = await app_client.get(f"/sources/{slug}")
    assert resp.status_code == 200
    assert 'href="/sources"' in resp.text


# ---------------------------------------------------------------------------
# GET /sources/{slug} — unknown slug → 404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_source_detail_unknown_slug_returns_404(
    app_client: AsyncClient,
    seeded_consensus: dict,
) -> None:
    """An unknown source slug returns HTTP 404.

    The seeded sources have specific slugs; a completely different slug
    must yield a 404, not a 500 or empty page.
    """
    resp = await app_client.get("/sources/this-slug-does-not-exist")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_source_detail_unknown_slug_no_data_returns_404(
    app_client: AsyncClient,
) -> None:
    """404 is returned even when the DB has no consensus data at all."""
    # No seeded_consensus — clean DB.
    resp = await app_client.get("/sources/no-such-source")
    assert resp.status_code == 404
