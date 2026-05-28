"""Integration tests for the homepage consensus hero section (#218).

Tests:
- Pre-lottery calendar stub → big-board content renders (heading, rows).
- Post-lottery calendar stub → mock-draft code path taken; with no mock data
  the empty/fallback state is rendered (the board_kind falls back to BIG_BOARD
  so the existing big-board rows are shown — this is the documented fallback).
- Empty year (no snapshot) → empty state renders gracefully, no crash.

The post-lottery / mock-draft live view has no data (pending the mock-
extraction ticket); the live behaviour is therefore verified via these stubs
rather than through a running browser.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
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


async def _make_board(
    db: AsyncSession,
    *,
    source: NewsSource,
    draft_year: int,
    entries: list[tuple[PlayerMaster, int]],
) -> Board:
    """Insert an APPROVED big-board with the given entries."""
    assert source.id is not None
    board = Board(
        news_source_id=source.id,
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
async def players(db_session: AsyncSession) -> list[PlayerMaster]:
    """Create three players for consensus seeding."""
    ps = [
        make_player("Cooper", "Flagg", school="Duke"),
        make_player("Dylan", "Harper", school="Rutgers"),
        make_player("Ace", "Bailey", school="Kansas"),
    ]
    for p in ps:
        db_session.add(p)
    await db_session.flush()
    return ps


@pytest_asyncio.fixture()
async def populated_big_board(
    db_session: AsyncSession,
    players: list[PlayerMaster],
) -> None:
    """Seed a big-board consensus snapshot for draft year 2026.

    Two boards from two different sources are required so that players
    clear the ``MIN_SOURCES = 2`` threshold and appear in the consensus
    output (``BigBoardConsensus`` rows are only written for players that
    appear in ≥2 sources).
    """
    source1 = await _make_source(db_session, "hero-test-source-1")
    source2 = await _make_source(db_session, "hero-test-source-2")
    entries = [(p, i + 1) for i, p in enumerate(players)]
    await _make_board(db_session, source=source1, draft_year=2026, entries=entries)
    await _make_board(db_session, source=source2, draft_year=2026, entries=entries)
    await db_session.commit()
    await svc.recompute_consensus(
        db_session, draft_year=2026, trigger=ConsensusTrigger.MANUAL
    )
    await db_session.commit()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_home_pre_lottery_renders_big_board(
    app_client: AsyncClient,
    populated_big_board: None,
    players: list[PlayerMaster],
) -> None:
    """Pre-lottery calendar date → homepage shows Big Board consensus heading and rows.

    Stubs ``get_consensus_board_kind`` to return BIG_BOARD (pre-lottery).
    Asserts the response HTML contains the board heading and player names.
    """
    pre_lottery_date = date(2026, 1, 1)  # well before the lottery

    with patch(
        "app.routes.ui.get_consensus_board_kind",
        return_value=BoardKind.BIG_BOARD,
    ):
        resp = await app_client.get("/")

    assert resp.status_code == 200
    html = resp.text

    # The consensus hero section must be present.
    assert "consensusHeroSection" in html
    # Heading should mention "Consensus Board" (big-board label).
    assert "Consensus Board" in html
    # At least the first player's name must appear.
    assert players[0].display_name in html
    # Photo + school-logo cells are wired in for every row. The inner <img>
    # tags only render when a real PlayerImageAsset or school logo exists —
    # the cell wrappers always render, so we assert on those.
    assert "consensus-hero__player-cell" in html
    assert "consensus-hero__school-cell" in html
    # The rank column header must be present.
    assert "consensus-hero__th--rank" in html or "#" in html
    # The avg / range / sources columns must be present.
    assert "Avg" in html
    assert "Range" in html


@pytest.mark.asyncio
async def test_home_post_lottery_falls_back_to_big_board(
    app_client: AsyncClient,
    populated_big_board: None,
    players: list[PlayerMaster],
) -> None:
    """Post-lottery stub → mock-draft path taken; no mock data → falls back to BIG_BOARD.

    The calendar would normally return MOCK_DRAFT; since no mock-draft
    consensus rows exist in the test DB the route's empty-fallback logic
    switches back to BIG_BOARD and renders the populated big-board rows.

    This is the documented fallback (see comment in ui.py home handler).
    """
    with patch(
        "app.routes.ui.get_consensus_board_kind",
        return_value=BoardKind.MOCK_DRAFT,
    ):
        resp = await app_client.get("/")

    assert resp.status_code == 200
    html = resp.text

    # The consensus hero section must still be present and populated.
    assert "consensusHeroSection" in html
    # Because no mock-draft data exists, the fallback renders the big board rows.
    assert players[0].display_name in html
    # Heading must match the data shown: big-board rows get the big-board
    # heading, never the mock-draft heading (regression: post-lottery phase
    # previously mislabeled big-board rows as "Consensus Mock Draft").
    assert "Consensus Board" in html
    assert "Consensus Mock Draft" not in html


@pytest.mark.asyncio
async def test_home_no_consensus_data_renders_empty_state(
    app_client: AsyncClient,
) -> None:
    """No consensus snapshot at all → empty state renders without crashing.

    No ``populated_big_board`` fixture; the DB has no consensus rows for 2026.
    The hero section must still render (the empty-state block) and return 200.
    """
    with patch(
        "app.routes.ui.get_consensus_board_kind",
        return_value=BoardKind.BIG_BOARD,
    ):
        resp = await app_client.get("/")

    assert resp.status_code == 200
    html = resp.text

    # The section must still be rendered (empty-state branch).
    assert "consensusHeroSection" in html
    # The empty message should be present.
    assert "consensus-hero__empty" in html


@pytest.mark.asyncio
async def test_home_consensus_single_snapshot_no_delta_arrows(
    app_client: AsyncClient,
    populated_big_board: None,
) -> None:
    """Single snapshot (no prior snapshot) → no up/down delta arrows shown.

    With only one snapshot, rank_delta is None for all rows, so the delta
    column should not contain any delta--up or delta--down spans.
    """
    with patch(
        "app.routes.ui.get_consensus_board_kind",
        return_value=BoardKind.BIG_BOARD,
    ):
        resp = await app_client.get("/")

    assert resp.status_code == 200
    html = resp.text

    # There should be no up/down delta elements when only one snapshot exists.
    assert "consensus-hero__delta--up" not in html
    assert "consensus-hero__delta--down" not in html


@pytest.mark.asyncio
async def test_home_consensus_rows_injected_as_window_consensus(
    app_client: AsyncClient,
    populated_big_board: None,
) -> None:
    """Consensus rows must be injected into window.consensus for client JS.

    The script block must contain ``window.consensus`` with the JSON payload.
    """
    with patch(
        "app.routes.ui.get_consensus_board_kind",
        return_value=BoardKind.BIG_BOARD,
    ):
        resp = await app_client.get("/")

    assert resp.status_code == 200
    html = resp.text

    assert "window.consensus" in html
    assert "window.BOARD_KIND" in html
