"""Integration tests for the player-detail page consensus block (#220).

Tests:
- Player WITH consensus: section renders with rank headline, source-breakdown
  table (>= 1 row), and an SVG history chart with a non-empty <path d=...>.
- Player WITHOUT consensus: consensus section is entirely absent (no card,
  no headline) — page still returns 200.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.boards import Board, BoardEntry, BoardStatus
from app.schemas.consensus import ConsensusTrigger
from app.schemas.news_items import NewsItem
from app.schemas.news_sources import FeedType, NewsSource
from app.schemas.players_master import PlayerMaster
from app.services import consensus_service as svc
from app.services import school_logo_service
from app.services.consensus_read_service import get_player_consensus_detail
from tests.integration.conftest import make_player


@pytest_asyncio.fixture(autouse=True)
async def _reset_logo_cache() -> AsyncGenerator[None, None]:
    """Clear the module-level school logo cache around each test."""
    school_logo_service.clear_cache()
    yield
    school_logo_service.clear_cache()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _make_source(db: AsyncSession, name: str) -> NewsSource:
    src = NewsSource(
        name=name,
        display_name=f"{name.replace('-', ' ').title()}",
        feed_type=FeedType.RSS,
        feed_url=f"https://example.com/{name}/feed.xml",
        is_active=True,
        fetch_interval_minutes=30,
    )
    db.add(src)
    await db.flush()
    return src


async def _make_news_item(
    db: AsyncSession, *, source: NewsSource, title: str, url: str
) -> int:
    """Insert a NewsItem and return its id (the source article for a board)."""
    assert source.id is not None
    item = NewsItem(
        source_id=source.id,
        external_id=url,
        title=title,
        url=url,
        published_at=_now() - timedelta(hours=25),
    )
    db.add(item)
    await db.flush()
    assert item.id is not None
    return item.id


async def _make_board(
    db: AsyncSession,
    *,
    source: NewsSource,
    draft_year: int,
    entries: list[tuple[PlayerMaster, int]],
    news_item_id: int | None = None,
) -> Board:
    """Insert an APPROVED big-board with the given entries.

    Pass ``news_item_id`` to back the board with a real source article (so the
    per-source breakdown can link out to it); omit it for a synthetic board.
    """
    assert source.id is not None
    board = Board(
        news_source_id=source.id,
        news_item_id=news_item_id,
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
async def board_player(db_session: AsyncSession) -> PlayerMaster:
    """Create a player that will appear on the consensus board."""
    p = make_player("Board", "Player", school="Duke")
    p.draft_year = 2026
    db_session.add(p)
    await db_session.flush()
    await db_session.commit()
    return p


@pytest_asyncio.fixture()
async def off_board_player(db_session: AsyncSession) -> PlayerMaster:
    """Create a player NOT on any board (no consensus data)."""
    p = make_player("Off", "Board", school="UNC")
    p.draft_year = 2026
    db_session.add(p)
    await db_session.flush()
    await db_session.commit()
    return p


@pytest_asyncio.fixture()
async def two_snapshot_consensus(
    db_session: AsyncSession,
    board_player: PlayerMaster,
) -> None:
    """Seed two consensus snapshots for board_player (produces a rank history).

    Two boards x two sources per snapshot to clear the MIN_SOURCES=2 threshold.
    The second snapshot runs after the first, so rank_delta is populated.
    """
    source1 = await _make_source(db_session, "consensus-source-a")
    source2 = await _make_source(db_session, "consensus-source-b")

    # Snapshot 1: board_player ranked #3 on both boards.
    entries = [(board_player, 3)]
    await _make_board(db_session, source=source1, draft_year=2026, entries=entries)
    await _make_board(db_session, source=source2, draft_year=2026, entries=entries)
    await db_session.commit()
    await svc.recompute_consensus(
        db_session, draft_year=2026, trigger=ConsensusTrigger.MANUAL
    )
    await db_session.commit()

    # Snapshot 2: board_player ranked #1 on both boards (risen).
    entries2 = [(board_player, 1)]
    await _make_board(db_session, source=source1, draft_year=2026, entries=entries2)
    await _make_board(db_session, source=source2, draft_year=2026, entries=entries2)
    await db_session.commit()
    await svc.recompute_consensus(
        db_session, draft_year=2026, trigger=ConsensusTrigger.MANUAL
    )
    await db_session.commit()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_player_with_consensus_shows_section(
    app_client: AsyncClient,
    two_snapshot_consensus: None,
    board_player: PlayerMaster,
) -> None:
    """Player on the consensus board → section renders with rank, sources, and SVG chart.

    Asserts:
    - HTTP 200.
    - 'Consensus rank: #X' headline present (via 'Consensus Big Board' heading).
    - Source-breakdown table has at least one source row.
    - SVG element present with a non-empty <path d=...> for the rank history.
    """
    assert board_player.slug is not None
    resp = await app_client.get(f"/players/{board_player.slug}")
    assert resp.status_code == 200
    html = resp.text

    # The consensus section heading must appear.
    assert "Consensus Big Board" in html

    # The rank number must appear (current rank after snapshot 2 is #1).
    assert "#1" in html

    # The consensusSection container must be present.
    assert "consensusSection" in html

    # Source table rows: at least the display names of the two sources.
    assert "Consensus Source A" in html or "consensus-sources__row" in html

    # SVG chart container must be present (JS populates <path>, but the <svg>
    # tag and the id must be in the HTML for JS to target).
    assert "consensusHistoryChart" in html

    # window.consensusHistory must be injected with >= 2 points.
    assert "consensusHistory" in html
    # Both snapshots → 2 history points in the JSON array.
    assert '"consensus_rank"' in html


@pytest.mark.asyncio
async def test_player_without_consensus_omits_section(
    app_client: AsyncClient,
    off_board_player: PlayerMaster,
) -> None:
    """Player not on any board → consensus section entirely absent; page returns 200.

    Asserts:
    - HTTP 200 (page does not 404 or error).
    - 'Consensus Big Board' heading absent.
    - 'consensusSection' id absent.
    - window.consensusHistory is null (not an array).
    """
    assert off_board_player.slug is not None
    resp = await app_client.get(f"/players/{off_board_player.slug}")
    assert resp.status_code == 200
    html = resp.text

    # No consensus section at all.
    assert "Consensus Big Board" not in html
    assert "consensusSection" not in html
    assert "consensusHistoryChart" not in html

    # window.consensusHistory injected as null.
    assert "consensusHistory = null" in html


@pytest.mark.asyncio
async def test_source_breakdown_links_to_mock_article(
    app_client: AsyncClient,
    db_session: AsyncSession,
    board_player: PlayerMaster,
) -> None:
    """A source whose board has a NewsItem exposes the article; synthetic does not.

    Two sources clear MIN_SOURCES=2. One board is backed by a real NewsItem
    (article-linkable); the other has no ``news_item_id`` (synthetic). The
    per-source breakdown should carry the article URL/title only for the former,
    and the player page should render the article URL as a link.
    """
    assert board_player.id is not None
    article_url = "https://example.substack.com/p/2026-big-board"
    article_title = "2026 NBA Draft Big Board"

    linked_source = await _make_source(db_session, "linked-source")
    plain_source = await _make_source(db_session, "plain-source")
    news_item_id = await _make_news_item(
        db_session, source=linked_source, title=article_title, url=article_url
    )
    entries = [(board_player, 4)]
    await _make_board(
        db_session,
        source=linked_source,
        draft_year=2026,
        entries=entries,
        news_item_id=news_item_id,
    )
    await _make_board(db_session, source=plain_source, draft_year=2026, entries=entries)
    await db_session.commit()
    await svc.recompute_consensus(
        db_session, draft_year=2026, trigger=ConsensusTrigger.MANUAL
    )
    await db_session.commit()

    detail = await get_player_consensus_detail(
        db_session, player_id=board_player.id, draft_year=2026
    )
    assert detail is not None
    by_source = {s.source_name: s for s in detail.source_ranks}
    assert by_source["linked-source"].article_url == article_url
    assert by_source["linked-source"].article_title == article_title
    # The synthetic board contributes a rank but no linkable article.
    assert by_source["plain-source"].article_url is None
    assert by_source["plain-source"].article_title is None

    # The page renders the article URL as a link.
    assert board_player.slug is not None
    resp = await app_client.get(f"/players/{board_player.slug}")
    assert resp.status_code == 200
    assert article_url in resp.text
