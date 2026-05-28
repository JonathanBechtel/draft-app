"""Integration tests for the homepage supporting panels (#219).

Panels:
- Biggest Movers: top risers/fallers by rank delta.
- Source Spotlight: most contrarian source callout.
- Board Freshness: boards/sources/last-updated summary.

Each panel has two test scenarios:
1. Data present → panel content rendered.
2. Data absent (empty DB or single snapshot) → graceful empty state.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator

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
    published_offset_hours: int = 24,
) -> Board:
    """Insert an APPROVED big-board with the given entries."""
    assert source.id is not None
    board = Board(
        news_source_id=source.id,
        draft_year=draft_year,
        published_at=_now() - timedelta(hours=published_offset_hours),
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
# Shared players fixture
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def players(db_session: AsyncSession) -> list[PlayerMaster]:
    """Create three players for consensus seeding."""
    ps = [
        make_player("Alpha", "Player", school="Duke"),
        make_player("Beta", "Player", school="Kentucky"),
        make_player("Gamma", "Player", school="Kansas"),
    ]
    for p in ps:
        db_session.add(p)
    await db_session.flush()
    return ps


# ---------------------------------------------------------------------------
# Two-snapshot fixture (for Biggest Movers)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def two_snapshot_board(
    db_session: AsyncSession,
    players: list[PlayerMaster],
) -> None:
    """Seed two consensus snapshots with rank changes so movers are non-empty.

    Snapshot 1: Alpha=1, Beta=2, Gamma=3
    Snapshot 2: Gamma=1 (+2), Beta=2 (flat), Alpha=3 (-2)

    After recompute, Alpha rank_delta = -2 (faller), Gamma rank_delta = +2 (riser).
    """
    source1 = await _make_source(db_session, "panels-test-src-1")
    source2 = await _make_source(db_session, "panels-test-src-2")

    alpha, beta, gamma = players

    # Snapshot 1: Alpha first
    entries_v1 = [(alpha, 1), (beta, 2), (gamma, 3)]
    await _make_board(
        db_session, source=source1, draft_year=2026, entries=entries_v1, published_offset_hours=48
    )
    await _make_board(
        db_session, source=source2, draft_year=2026, entries=entries_v1, published_offset_hours=48
    )
    await db_session.commit()
    await svc.recompute_consensus(
        db_session, draft_year=2026, trigger=ConsensusTrigger.MANUAL
    )
    await db_session.commit()

    # Snapshot 2: Gamma jumps to first, Alpha drops to third
    entries_v2 = [(gamma, 1), (beta, 2), (alpha, 3)]
    await _make_board(
        db_session, source=source1, draft_year=2026, entries=entries_v2, published_offset_hours=2
    )
    await _make_board(
        db_session, source=source2, draft_year=2026, entries=entries_v2, published_offset_hours=2
    )
    await db_session.commit()
    await svc.recompute_consensus(
        db_session, draft_year=2026, trigger=ConsensusTrigger.MANUAL
    )
    await db_session.commit()


# ---------------------------------------------------------------------------
# Single-snapshot fixture (Spotlight + Freshness data, Movers empty)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def single_snapshot_board(
    db_session: AsyncSession,
    players: list[PlayerMaster],
) -> None:
    """Seed a single consensus snapshot (no prior, so Movers will be empty).

    Spotlight and Freshness panels should render because SourceAnalytics and
    APPROVED boards exist.
    """
    source1 = await _make_source(db_session, "panels-single-src-1")
    source2 = await _make_source(db_session, "panels-single-src-2")
    entries = [(p, i + 1) for i, p in enumerate(players)]
    await _make_board(db_session, source=source1, draft_year=2026, entries=entries)
    await _make_board(db_session, source=source2, draft_year=2026, entries=entries)
    await db_session.commit()
    await svc.recompute_consensus(
        db_session, draft_year=2026, trigger=ConsensusTrigger.MANUAL
    )
    await db_session.commit()


# ---------------------------------------------------------------------------
# Biggest Movers tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_biggest_movers_populated(
    app_client: AsyncClient,
    two_snapshot_board: None,
    players: list[PlayerMaster],
) -> None:
    """Two snapshots with rank changes → Biggest Movers panel shows risers/fallers.

    After snapshot 2 Gamma moved up 2 spots (riser) and Alpha dropped 2 (faller).
    Asserts the panel is present and contains at least one mover entry.
    """
    resp = await app_client.get("/")
    assert resp.status_code == 200
    html = resp.text

    assert "biggestMoversPanel" in html
    # The panel should show riser or faller entries (not the empty-state message)
    assert "Movement data available once a second snapshot is computed." not in html
    # At least one delta arrow class must be present
    assert (
        "consensus-panel__mover--up" in html or "consensus-panel__mover--down" in html
    )


@pytest.mark.asyncio
async def test_biggest_movers_empty_single_snapshot(
    app_client: AsyncClient,
    single_snapshot_board: None,
) -> None:
    """Single snapshot → no rank_delta → Biggest Movers shows graceful empty state.

    The panel must still render (no crash) with the empty-state message.
    """
    resp = await app_client.get("/")
    assert resp.status_code == 200
    html = resp.text

    assert "biggestMoversPanel" in html
    assert "Movement data available once a second snapshot is computed." in html


@pytest.mark.asyncio
async def test_biggest_movers_empty_no_snapshot(
    app_client: AsyncClient,
) -> None:
    """No snapshot at all → Biggest Movers shows graceful empty state.

    The hero and panels must all render; no 500 error.
    """
    resp = await app_client.get("/")
    assert resp.status_code == 200
    html = resp.text

    assert "biggestMoversPanel" in html
    assert "Movement data available once a second snapshot is computed." in html


# ---------------------------------------------------------------------------
# Source Spotlight tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_source_spotlight_populated(
    app_client: AsyncClient,
    single_snapshot_board: None,
) -> None:
    """Single snapshot with SourceAnalytics → Source Spotlight panel renders.

    The panel should display the contrarian source name and deviation figure.
    """
    resp = await app_client.get("/")
    assert resp.status_code == 200
    html = resp.text

    assert "sourceSpotlightPanel" in html
    # The callout text must be present (not the empty state)
    assert "Most contrarian source this week" in html
    # Should not show the empty-state message
    assert "Source analytics are not yet available." not in html


@pytest.mark.asyncio
async def test_source_spotlight_empty_no_snapshot(
    app_client: AsyncClient,
) -> None:
    """No snapshot → Source Spotlight shows graceful empty state without crashing."""
    resp = await app_client.get("/")
    assert resp.status_code == 200
    html = resp.text

    assert "sourceSpotlightPanel" in html
    assert "Source analytics are not yet available." in html


# ---------------------------------------------------------------------------
# Board Freshness tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_board_freshness_populated(
    app_client: AsyncClient,
    single_snapshot_board: None,
) -> None:
    """Single snapshot with approved boards → Board Freshness panel renders.

    Should display a boards/sources count and a last-updated date.
    """
    resp = await app_client.get("/")
    assert resp.status_code == 200
    html = resp.text

    assert "boardFreshnessPanel" in html
    # The summary text must be present (not the empty state)
    assert "Based on" in html
    assert "Last updated" in html
    # Should not show the empty-state message
    assert "No approved boards available yet." not in html


@pytest.mark.asyncio
async def test_board_freshness_empty_no_snapshot(
    app_client: AsyncClient,
) -> None:
    """No snapshot → Board Freshness shows graceful empty state without crashing."""
    resp = await app_client.get("/")
    assert resp.status_code == 200
    html = resp.text

    assert "boardFreshnessPanel" in html
    assert "No approved boards available yet." in html


# ---------------------------------------------------------------------------
# Hero is not broken by panels
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_panels_do_not_break_consensus_hero(
    app_client: AsyncClient,
    two_snapshot_board: None,
    players: list[PlayerMaster],
) -> None:
    """All three panels present → the consensus hero above them still renders.

    Regression guard: panels must not displace or break the hero section.
    """
    resp = await app_client.get("/")
    assert resp.status_code == 200
    html = resp.text

    # Hero section must be present
    assert "consensusHeroSection" in html
    # At least one player row must appear (the hero has data)
    assert players[0].display_name in html or players[2].display_name in html
    # All three panel IDs must also be present
    assert "biggestMoversPanel" in html
    assert "sourceSpotlightPanel" in html
    assert "boardFreshnessPanel" in html
