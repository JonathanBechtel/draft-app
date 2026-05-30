"""Integration tests for the player rank trajectories section on /consensus (ticket #276).

Covers:
- Multi-snapshot data renders the full SVG chart with player polylines + legend.
- Risers/fallers are color-direction-coded in the legend.
- A single snapshot renders the flat ("appears once multiple snapshots exist") state.
- No data renders the empty state gracefully (no 500).

Snapshots are inserted directly (mirroring tests/integration/test_consensus_page_service.py)
so the read-path is exercised deterministically without depending on the
multi-source consensus algorithm.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.boards import Board, BoardEntry, BoardKind, BoardStatus
from app.schemas.consensus import (
    BigBoardConsensus,
    ConsensusSnapshot,
    ConsensusTrigger,
    SourceAnalytics,
)
from app.schemas.news_sources import FeedType, NewsSource
from app.schemas.players_master import PlayerMaster
from tests.integration.conftest import make_player


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _make_source(db: AsyncSession, name: str) -> NewsSource:
    src = NewsSource(
        name=name,
        display_name=name,
        feed_type=FeedType.RSS,
        feed_url=f"https://example.com/{name}/feed.xml",
        is_active=True,
        fetch_interval_minutes=30,
    )
    db.add(src)
    await db.flush()
    return src


async def _make_board_with_entries(
    db: AsyncSession,
    *,
    source: NewsSource,
    draft_year: int,
    entries: list[tuple[PlayerMaster, int]],
) -> Board:
    assert source.id is not None
    board = Board(
        news_source_id=source.id,
        draft_year=draft_year,
        published_at=_now() - timedelta(days=1),
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


async def _make_snapshot(
    db: AsyncSession,
    *,
    draft_year: int,
    board_ids: list[int],
    bbc_rows: list[tuple[PlayerMaster, int]],
    sources: list[tuple[NewsSource, int]],
    computed_hours_ago: float,
) -> ConsensusSnapshot:
    """Insert a ConsensusSnapshot + its BigBoardConsensus / SourceAnalytics rows."""
    snap = ConsensusSnapshot(
        draft_year=draft_year,
        computed_at=_now() - timedelta(hours=computed_hours_ago),
        num_boards=len(board_ids),
        board_ids=board_ids,
        trigger=ConsensusTrigger.MANUAL,
    )
    db.add(snap)
    await db.flush()
    assert snap.id is not None

    for player, consensus_rank in bbc_rows:
        assert player.id is not None
        db.add(
            BigBoardConsensus(
                snapshot_id=snap.id,
                draft_year=draft_year,
                player_id=player.id,
                consensus_rank=consensus_rank,
                avg_rank=float(consensus_rank),
                median_rank=float(consensus_rank),
                high_rank=consensus_rank,
                low_rank=consensus_rank,
                std_dev=0.0,
                num_sources=len(board_ids),
            )
        )

    for src, board_id in sources:
        assert src.id is not None
        db.add(
            SourceAnalytics(
                snapshot_id=snap.id,
                news_source_id=src.id,
                latest_board_id=board_id,
                avg_deviation=0.0,
                contrarian_score=0.0,
                biggest_outlier_player_id=None,
                outlier_delta=0,
            )
        )

    await db.flush()
    return snap


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def multi_snapshot(db_session: AsyncSession) -> dict:
    """Two snapshots so trajectory series have length 2 (a riser and a faller).

    Snapshot 1 (2h ago): p1=1, p2=2, p3=3.
    Snapshot 2 (now):     p3=1, p2=2, p1=3  -> p3 rises, p1 falls.
    """
    p1 = make_player("Adam", "Ascend", school="Duke")
    p2 = make_player("Ben", "Bench", school="Kansas")
    p3 = make_player("Cody", "Climb", school="UNC")
    for p in (p1, p2, p3):
        db_session.add(p)
    await db_session.flush()

    s1 = await _make_source(db_session, "traj-alpha")
    s2 = await _make_source(db_session, "traj-beta")
    board_a = await _make_board_with_entries(
        db_session, source=s1, draft_year=2026, entries=[(p3, 1), (p2, 2), (p1, 3)]
    )
    board_b = await _make_board_with_entries(
        db_session, source=s2, draft_year=2026, entries=[(p3, 1), (p2, 2), (p1, 3)]
    )
    assert board_a.id is not None and board_b.id is not None
    board_ids = [board_a.id, board_b.id]
    srcs = [(s1, board_a.id), (s2, board_b.id)]

    await _make_snapshot(
        db_session,
        draft_year=2026,
        board_ids=board_ids,
        bbc_rows=[(p1, 1), (p2, 2), (p3, 3)],
        sources=srcs,
        computed_hours_ago=2,
    )
    await _make_snapshot(
        db_session,
        draft_year=2026,
        board_ids=board_ids,
        bbc_rows=[(p3, 1), (p2, 2), (p1, 3)],
        sources=srcs,
        computed_hours_ago=0,
    )
    await db_session.commit()
    return {"players": [p1, p2, p3], "sources": [s1, s2]}


@pytest_asyncio.fixture()
async def single_snapshot(db_session: AsyncSession) -> dict:
    """Exactly one snapshot -> each series has length 1 -> flat state."""
    p1 = make_player("Solo", "Snap", school="Duke")
    p2 = make_player("Mono", "Shot", school="Kansas")
    for p in (p1, p2):
        db_session.add(p)
    await db_session.flush()

    s1 = await _make_source(db_session, "single-alpha")
    s2 = await _make_source(db_session, "single-beta")
    board_a = await _make_board_with_entries(
        db_session, source=s1, draft_year=2026, entries=[(p1, 1), (p2, 2)]
    )
    board_b = await _make_board_with_entries(
        db_session, source=s2, draft_year=2026, entries=[(p1, 1), (p2, 2)]
    )
    assert board_a.id is not None and board_b.id is not None

    await _make_snapshot(
        db_session,
        draft_year=2026,
        board_ids=[board_a.id, board_b.id],
        bbc_rows=[(p1, 1), (p2, 2)],
        sources=[(s1, board_a.id), (s2, board_b.id)],
        computed_hours_ago=0,
    )
    await db_session.commit()
    return {"players": [p1, p2], "sources": [s1, s2]}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trajectories_states(
    app_client: AsyncClient,
    multi_snapshot: dict,
) -> None:
    """Two+ snapshots render the full SVG chart with player lines and a legend."""
    with patch(
        "app.routes.ui.get_consensus_board_kind",
        return_value=BoardKind.BIG_BOARD,
    ):
        resp = await app_client.get("/consensus")
    assert resp.status_code == 200
    html = resp.text

    # Section + full chart present (not the empty/flat variant).
    assert "traj-section" in html
    assert "traj-svg" in html
    assert "traj__line" in html  # at least one player polyline
    assert "traj__legend" in html

    # Players appear in the legend.
    assert "Adam" in html
    assert "Cody" in html

    # Riser + faller direction classes are present (p3 rose, p1 fell).
    assert "traj__legend-item--up" in html
    assert "traj__legend-item--down" in html


@pytest.mark.asyncio
async def test_trajectories_single_snapshot_flat(
    app_client: AsyncClient,
    single_snapshot: dict,
) -> None:
    """A single snapshot renders the flat state, not the full chart."""
    with patch(
        "app.routes.ui.get_consensus_board_kind",
        return_value=BoardKind.BIG_BOARD,
    ):
        resp = await app_client.get("/consensus")
    assert resp.status_code == 200
    html = resp.text

    assert "traj-section" in html
    # Flat message shown; no SVG chart rendered.
    assert "traj-card__flat-msg" in html
    assert "traj-svg" not in html


@pytest.mark.asyncio
async def test_trajectories_empty_state(
    app_client: AsyncClient,
) -> None:
    """No consensus data renders the empty trajectories state without error."""
    resp = await app_client.get("/consensus")
    assert resp.status_code == 200
    html = resp.text

    assert "traj-section" in html
    assert "traj-card__empty-msg" in html
    assert "traj-svg" not in html
