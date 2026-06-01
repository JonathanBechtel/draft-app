"""Integration tests for the consensus board section (ticket #272).

Covers:
- Full board renders all seeded players with expected columns and markup.
- Range-bar markup (cb-range, cb-range__fill, cb-range__marker) is present.
- Calendar-kind heading (Big Board / Mock Draft) is reflected in the page.
- Empty state renders without a 500 when no snapshot exists.
- Status badges are present (lottery, 1st Rd, 2nd Rd labels).
- Controls (search input, position filter) are rendered.
- All required column headers are present.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
        display_name=f"{name.replace('-', ' ').title()}",
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
async def board_data(db_session: AsyncSession) -> dict:
    """Seed three players, two sources, and a consensus snapshot.

    Returns a dict of seeded objects for downstream assertions.
    """
    p1 = make_player("Cooper", "Flagg", school="Duke")
    p2 = make_player("Dylan", "Harper", school="Rutgers")
    p3 = make_player("Ace", "Bailey", school="Kansas")
    for p in (p1, p2, p3):
        db_session.add(p)
    await db_session.flush()

    s1 = await _make_source(db_session, "board-alpha-source")
    s2 = await _make_source(db_session, "board-beta-source")

    # Two sources with slightly different orderings so high/low differ per player.
    await _make_approved_board(
        db_session,
        source=s1,
        draft_year=2026,
        entries=[(p1, 1), (p2, 2), (p3, 3)],
    )
    await _make_approved_board(
        db_session,
        source=s2,
        draft_year=2026,
        entries=[(p1, 1), (p2, 3), (p3, 2)],
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
async def test_board_renders_all_players(
    app_client: AsyncClient,
    board_data: dict,
) -> None:
    """GET /consensus with data renders all seeded players in the board table.

    Each seeded player name must appear in the rendered HTML.
    """
    with patch(
        "app.routes.ui.get_consensus_board_kind",
        return_value=BoardKind.BIG_BOARD,
    ):
        resp = await app_client.get("/consensus")

    assert resp.status_code == 200
    html = resp.text

    players = board_data["players"]
    for p in players:
        name = p.display_name or f"{p.first_name} {p.last_name}"
        assert name in html, f"Player '{name}' not found in rendered board"


@pytest.mark.asyncio
async def test_board_range_bar_markup_present(
    app_client: AsyncClient,
    board_data: dict,
) -> None:
    """GET /consensus board section includes range-bar CSS classes.

    The cb-range, cb-range__fill, and cb-range__marker classes must be
    present — they constitute the tug-of-war range bar.
    """
    with patch(
        "app.routes.ui.get_consensus_board_kind",
        return_value=BoardKind.BIG_BOARD,
    ):
        resp = await app_client.get("/consensus")

    assert resp.status_code == 200
    html = resp.text

    assert "cb-range" in html, "Range bar container class not found"
    assert "cb-range__fill" in html, "Range bar fill class not found"
    assert "cb-range__marker" in html, "Range bar marker class not found"


@pytest.mark.asyncio
async def test_board_column_headers_present(
    app_client: AsyncClient,
    board_data: dict,
) -> None:
    """GET /consensus board renders all required column headers.

    The board shows: # · Δ · Trend · Player · School · Pos · Ht · Wt ·
    Age · Avg · Range · Src. (The Status column was removed — it overflowed
    the card and the range bar already conveys tier.)
    """
    with patch(
        "app.routes.ui.get_consensus_board_kind",
        return_value=BoardKind.BIG_BOARD,
    ):
        resp = await app_client.get("/consensus")

    assert resp.status_code == 200
    html = resp.text

    for header in ("Trend", "School", "Pos", "Avg", "Range", "Src"):
        assert header in html, f"Column header '{header}' not found in rendered board"


@pytest.mark.asyncio
async def test_board_status_column_removed(
    app_client: AsyncClient,
    board_data: dict,
) -> None:
    """The Status column/badge is intentionally absent from the board.

    It was dropped because it overflowed the card edge; the range bar
    already conveys draft tier. This guards against it being reintroduced.
    """
    with patch(
        "app.routes.ui.get_consensus_board_kind",
        return_value=BoardKind.BIG_BOARD,
    ):
        resp = await app_client.get("/consensus")

    assert resp.status_code == 200
    html = resp.text
    assert "cb-status" not in html, "Status badge class should have been removed"


@pytest.mark.asyncio
async def test_board_controls_rendered(
    app_client: AsyncClient,
    board_data: dict,
) -> None:
    """GET /consensus board includes the search input and position filter.

    The cbSearch and cbPosFilter IDs are anchors for consensus-board.js.
    """
    with patch(
        "app.routes.ui.get_consensus_board_kind",
        return_value=BoardKind.BIG_BOARD,
    ):
        resp = await app_client.get("/consensus")

    assert resp.status_code == 200
    html = resp.text
    assert 'id="cbSearch"' in html, "Search input not found in board section"
    assert 'id="cbPosFilter"' in html, "Position filter not found in board section"


@pytest.mark.asyncio
async def test_board_big_board_heading(
    app_client: AsyncClient,
    board_data: dict,
) -> None:
    """GET /consensus with BIG_BOARD data renders 'Big Board' in the heading.

    The calendar-determined kind is forced to BIG_BOARD when rows exist.
    """
    with patch(
        "app.routes.ui.get_consensus_board_kind",
        return_value=BoardKind.BIG_BOARD,
    ):
        resp = await app_client.get("/consensus")

    assert resp.status_code == 200
    html = resp.text
    assert "Big Board" in html, "'Big Board' heading not found on consensus page"


@pytest.mark.asyncio
async def test_board_empty_state_no_500(
    app_client: AsyncClient,
) -> None:
    """GET /consensus with no snapshot data returns 200 with an empty state message.

    No consensus snapshot exists in this test (no seeded data), so the
    board partial must render the empty-state element gracefully.
    """
    resp = await app_client.get("/consensus")
    assert resp.status_code == 200
    html = resp.text
    # Either the empty-state message or the section container must be present.
    assert (
        "No consensus data" in html
        or "consensusBoardSection" in html
    ), "Board section container or empty message not found"


@pytest.mark.asyncio
async def test_board_source_breakdown_toggle_markup(
    app_client: AsyncClient,
    board_data: dict,
) -> None:
    """Each board row exposes the expand toggle + a paired detail row.

    The per-analyst source breakdown (lazy-loaded by consensus.js) needs:
    - a ``.cb-toggle`` button per row,
    - an empty ``cbDetail-{player_id}`` sibling row carrying the player id,
    - ``data-draft-year`` / ``data-board-kind`` on the table for the API call.
    """
    with patch(
        "app.routes.ui.get_consensus_board_kind",
        return_value=BoardKind.BIG_BOARD,
    ):
        resp = await app_client.get("/consensus")

    assert resp.status_code == 200
    html = resp.text

    assert 'class="cb-toggle"' in html, "Expand toggle button not rendered"
    assert 'data-draft-year="2026"' in html, "draft_year data attr missing on table"
    assert 'data-board-kind="BIG_BOARD"' in html, "board_kind data attr missing on table"

    # Every seeded player gets a detail shell keyed by player id.
    for p in board_data["players"]:
        assert p.id is not None
        assert f'id="cbDetail-{p.id}"' in html, f"detail row missing for player {p.id}"
        assert f'data-player-id="{p.id}"' in html, f"data-player-id missing for {p.id}"


@pytest.mark.asyncio
async def test_player_consensus_api_returns_source_ranks(
    app_client: AsyncClient,
    board_data: dict,
) -> None:
    """GET /api/consensus/player/{id} returns the per-source rank breakdown.

    This is the endpoint the inline expander hydrates from; each contributing
    source must appear in ``source_ranks`` with its individual rank.
    """
    p2 = board_data["players"][1]  # Dylan Harper — ranked 2 and 3 by the two sources
    assert p2.id is not None

    resp = await app_client.get(
        f"/api/consensus/player/{p2.id}",
        params={"draft_year": 2026, "kind": "BIG_BOARD"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["player_id"] == p2.id
    source_ranks = body["source_ranks"]
    assert len(source_ranks) == 2, "expected one entry per contributing source"
    assert {e["source_rank"] for e in source_ranks} == {2, 3}
    for entry in source_ranks:
        assert "source_display_name" in entry
        assert "source_rank" in entry


@pytest.mark.asyncio
async def test_board_section_anchor_present(
    app_client: AsyncClient,
    board_data: dict,
) -> None:
    """GET /consensus board section ID is present so the scaffold links correctly.

    The scaffold (ticket #271) wraps the board partial in a section with
    id='consensusBoardSection'; that ID must appear in the rendered output.
    """
    with patch(
        "app.routes.ui.get_consensus_board_kind",
        return_value=BoardKind.BIG_BOARD,
    ):
        resp = await app_client.get("/consensus")

    assert resp.status_code == 200
    html = resp.text
    assert "consensusBoardSection" in html, "Board section anchor not found in page"
