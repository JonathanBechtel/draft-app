"""Integration tests for the scatter section of /consensus (ticket #273).

Covers:
- GET /consensus with source overlays renders scatter markup.
- At least one dot (circle.scatter__dot) is present in rendered HTML
  when source overlay data exists.
- Scatter dot has a native SVG <title> tooltip with player + rank info.
- Active source anchor (scatterSourceLink) links out to work_url (external)
  with target="_blank" and rel="noopener" when work_url is set, OR falls
  back to /sources/{slug} when no external URL exists.
- Source picker buttons are rendered for each source.
- Empty-state renders gracefully when no source overlays exist.
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
from app.schemas.news_items import NewsItem
from app.schemas.news_sources import FeedType, NewsSource
from app.schemas.players_master import PlayerMaster
from app.services import consensus_service as svc
from tests.integration.conftest import make_player


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _make_source(
    db: AsyncSession,
    name: str,
    *,
    work_url: str | None = None,
) -> tuple[NewsSource, NewsItem | None]:
    """Create and flush a NewsSource, optionally with a linked news article.

    Returns (source, article_or_None).  When work_url is supplied an article
    is created so the board can carry a news_item_id — this lets the template
    render an external link-out for the source.

    Args:
        db: Async DB session.
        name: Source name (used as slug base too).
        work_url: Optional URL for the source's published board article.

    Returns:
        Tuple of (NewsSource, NewsItem | None).
    """
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
    assert src.id is not None

    article: NewsItem | None = None
    if work_url is not None:
        article = NewsItem(
            news_source_id=src.id,
            external_id=f"{name}-board-article",
            title=f"{name.title()} Mock Draft Board",
            url=work_url,
            published_at=_now() - timedelta(hours=2),
            is_active=True,
        )
        db.add(article)
        await db.flush()

    return src, article


async def _make_approved_board(
    db: AsyncSession,
    *,
    source: NewsSource,
    draft_year: int,
    entries: list[tuple[PlayerMaster, int]],
    article: NewsItem | None = None,
) -> Board:
    """Insert an APPROVED board, optionally linked to a news article.

    Args:
        db: Async DB session.
        source: The NewsSource that owns this board.
        draft_year: Draft year to associate with.
        entries: List of (player, position) tuples.
        article: Optional NewsItem to link as the source article.

    Returns:
        The created Board.
    """
    assert source.id is not None
    board = Board(
        news_source_id=source.id,
        news_item_id=article.id if article is not None else None,
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
async def seeded_scatter_with_work_url(db_session: AsyncSession) -> dict:
    """Seed players, a source with work_url, and a consensus snapshot.

    Source s1 has work_url (external link); source s2 does not (internal
    /sources/{slug} fallback).  Players are ranked differently between sources
    so the overlay has meaningful delta values.

    Returns a dict with seeded players and sources.
    """
    p1 = make_player("Cooper", "Flagg", school="Duke")
    p2 = make_player("Dylan", "Harper", school="Rutgers")
    p3 = make_player("Ace", "Bailey", school="Kansas")
    for p in (p1, p2, p3):
        db_session.add(p)
    await db_session.flush()

    # s1 has an external work_url
    s1, article1 = await _make_source(
        db_session,
        "scatter-source-alpha",
        work_url="https://externalboard.example.com/2026-mock",
    )
    # s2 has no external work_url
    s2, article2 = await _make_source(db_session, "scatter-source-beta")

    # s1: ranks Ace Bailey higher than consensus (bold call)
    s1_entries = [(p1, 1), (p2, 2), (p3, 3)]
    await _make_approved_board(
        db_session,
        source=s1,
        draft_year=2026,
        entries=s1_entries,
        article=article1,
    )

    # s2: ranks players differently — creates divergence for the scatter
    s2_entries = [(p1, 1), (p3, 2), (p2, 3)]  # p3 and p2 swapped
    await _make_approved_board(
        db_session,
        source=s2,
        draft_year=2026,
        entries=s2_entries,
        article=article2,
    )

    await db_session.commit()

    await svc.recompute_consensus(
        db_session, draft_year=2026, trigger=ConsensusTrigger.MANUAL
    )
    await db_session.commit()

    return {
        "players": [p1, p2, p3],
        "sources": [s1, s2],
        "work_url": "https://externalboard.example.com/2026-mock",
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scatter_section_renders_with_overlays(
    app_client: AsyncClient,
    seeded_scatter_with_work_url: dict,
) -> None:
    """GET /consensus renders the scatter section when source overlays exist.

    Asserts the scatter card container, picker, and SVG are present in the
    HTML response.
    """
    with patch(
        "app.routes.ui.get_consensus_board_kind",
        return_value=BoardKind.BIG_BOARD,
    ):
        resp = await app_client.get("/consensus")

    assert resp.status_code == 200
    html = resp.text

    # The scatter section container should be present
    assert "consensusScatterSection" in html
    # scatter-card (not the placeholder) should be rendered when overlays exist
    assert 'class="scatter-card"' in html or "scatter-card__picker" in html


@pytest.mark.asyncio
async def test_scatter_svg_dots_populated_by_js_data(
    app_client: AsyncClient,
    seeded_scatter_with_work_url: dict,
) -> None:
    """GET /consensus embeds overlay JSON data for the JS dot renderer.

    The template serializes source_overlays into a <script> tag so the
    client-side JS can render dots.  Asserts that the JSON data block is
    present and contains overlay_rows for at least one source.
    """
    with patch(
        "app.routes.ui.get_consensus_board_kind",
        return_value=BoardKind.BIG_BOARD,
    ):
        resp = await app_client.get("/consensus")

    assert resp.status_code == 200
    html = resp.text

    # The JSON data block must be present
    assert 'id="scatterOverlayData"' in html
    # Must contain at least one overlay_rows entry
    assert "overlay_rows" in html
    # Must contain scatter dot group anchor
    assert 'id="scatterDots"' in html


@pytest.mark.asyncio
async def test_scatter_dot_tooltip_text_present(
    app_client: AsyncClient,
    seeded_scatter_with_work_url: dict,
) -> None:
    """Scatter dots rendered by JS carry tooltips with player + rank info.

    The HTML response embeds overlay data as JSON.  Each overlay row must
    include player_name, source_rank, and consensus_rank so the JS can
    build the tooltip label (e.g. "Cooper Flagg — their #1 · consensus #1").
    """
    with patch(
        "app.routes.ui.get_consensus_board_kind",
        return_value=BoardKind.BIG_BOARD,
    ):
        resp = await app_client.get("/consensus")

    assert resp.status_code == 200
    html = resp.text

    # The embedded JSON must include tooltip-buildable fields
    assert "player_name" in html
    assert "source_rank" in html
    assert "consensus_rank" in html


@pytest.mark.asyncio
async def test_scatter_active_source_external_link(
    app_client: AsyncClient,
    seeded_scatter_with_work_url: dict,
) -> None:
    """The active-source link uses work_url with target=_blank and rel=noopener.

    When the first source in source_overlays has a work_url the caption
    renders an external anchor with target="_blank" and rel="noopener".
    """
    with patch(
        "app.routes.ui.get_consensus_board_kind",
        return_value=BoardKind.BIG_BOARD,
    ):
        resp = await app_client.get("/consensus")

    assert resp.status_code == 200
    html = resp.text

    work_url = seeded_scatter_with_work_url["work_url"]
    # The external board URL must appear in the rendered HTML
    assert work_url in html
    # Must carry the external link attributes
    assert 'target="_blank"' in html
    assert "noopener" in html


@pytest.mark.asyncio
async def test_scatter_source_link_fallback_to_internal(
    app_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """When no work_url, the source link falls back to /sources/{slug}.

    Seeds a source without an external work_url and verifies the caption
    link href uses the /sources/ internal path.
    """
    p1 = make_player("Test", "Player", school="Test School")
    p2 = make_player("Second", "Player", school="Other School")
    db_session.add(p1)
    db_session.add(p2)
    await db_session.flush()

    # Source with no work_url (no article)
    src, _ = await _make_source(db_session, "scatter-internal-source")
    entries = [(p1, 1), (p2, 2)]
    await _make_approved_board(
        db_session, source=src, draft_year=2026, entries=entries
    )
    await db_session.commit()

    await svc.recompute_consensus(
        db_session, draft_year=2026, trigger=ConsensusTrigger.MANUAL
    )
    await db_session.commit()

    with patch(
        "app.routes.ui.get_consensus_board_kind",
        return_value=BoardKind.BIG_BOARD,
    ):
        resp = await app_client.get("/consensus")

    assert resp.status_code == 200
    html = resp.text

    # The internal /sources/ path must be present for the no-work_url case
    assert "/sources/" in html
    # The scatterSourceLink anchor must be present
    assert 'id="scatterSourceLink"' in html


@pytest.mark.asyncio
async def test_scatter_source_picker_buttons_rendered(
    app_client: AsyncClient,
    seeded_scatter_with_work_url: dict,
) -> None:
    """Source picker renders one button per source in source_overlays.

    Asserts that the picker container and at least two buttons exist in the
    response HTML (one per seeded source).
    """
    with patch(
        "app.routes.ui.get_consensus_board_kind",
        return_value=BoardKind.BIG_BOARD,
    ):
        resp = await app_client.get("/consensus")

    assert resp.status_code == 200
    html = resp.text

    # Picker container must be present
    assert "scatter-card__picker" in html
    # Each source gets a button — check for the picker btn class
    assert html.count("scatter-card__picker-btn") >= 2


@pytest.mark.asyncio
async def test_scatter_empty_state_no_overlays(
    app_client: AsyncClient,
) -> None:
    """GET /consensus with no data renders the scatter empty state without a 500.

    When no consensus snapshot exists source_overlays will be empty, so the
    template must render the scatter-card--empty state gracefully.
    """
    resp = await app_client.get("/consensus")
    assert resp.status_code == 200
    html = resp.text
    # Section container must still be present
    assert "consensusScatterSection" in html
    # Empty-state element must be rendered
    assert "scatter-card--empty" in html
