"""Schema-level guards on the Phase 2 consensus tables.

These tests don't exercise a recompute service (that's slice 2b); they
just confirm the DB-level uniqueness constraints are wired up, so a
buggy recompute can't silently insert duplicate per-player or
per-source rows.
"""

from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.consensus import (
    BigBoardConsensus,
    ConsensusSnapshot,
    ConsensusTrigger,
    SourceAnalytics,
)
from app.schemas.news_sources import FeedType, NewsSource
from app.schemas.players_master import PlayerMaster
from tests.integration.conftest import make_player


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest_asyncio.fixture()
async def snapshot(db_session: AsyncSession) -> ConsensusSnapshot:
    snap = ConsensusSnapshot(
        draft_year=2026,
        computed_at=_now(),
        num_boards=1,
        board_ids=[],
        trigger=ConsensusTrigger.MANUAL,
    )
    db_session.add(snap)
    await db_session.flush()
    return snap


@pytest_asyncio.fixture()
async def two_players(db_session: AsyncSession) -> list[PlayerMaster]:
    rows = [
        make_player("Cooper", "Flagg", school="Duke"),
        make_player("Dylan", "Harper", school="Rutgers"),
    ]
    for p in rows:
        db_session.add(p)
    await db_session.flush()
    return rows


@pytest_asyncio.fixture()
async def two_sources(db_session: AsyncSession) -> list[NewsSource]:
    rows = [
        NewsSource(
            name="src-a",
            display_name="Source A",
            feed_type=FeedType.RSS,
            feed_url="https://example.com/a-feed",
            is_active=True,
            fetch_interval_minutes=30,
        ),
        NewsSource(
            name="src-b",
            display_name="Source B",
            feed_type=FeedType.RSS,
            feed_url="https://example.com/b-feed",
            is_active=True,
            fetch_interval_minutes=30,
        ),
    ]
    for s in rows:
        db_session.add(s)
    await db_session.flush()
    return rows


def _consensus_row(
    snapshot_id: int, player_id: int, rank: int
) -> BigBoardConsensus:
    return BigBoardConsensus(
        snapshot_id=snapshot_id,
        draft_year=2026,
        player_id=player_id,
        consensus_rank=rank,
        avg_rank=float(rank),
        median_rank=float(rank),
        high_rank=rank,
        low_rank=rank,
        std_dev=0.0,
        num_sources=1,
    )


@pytest.mark.asyncio
async def test_big_board_consensus_rejects_duplicate_player_per_snapshot(
    db_session: AsyncSession,
    snapshot: ConsensusSnapshot,
    two_players: list[PlayerMaster],
) -> None:
    """uq_big_board_consensus_snapshot_player blocks two rows for the same player."""
    snap_id = snapshot.id
    pid = two_players[0].id
    assert snap_id is not None and pid is not None

    db_session.add(_consensus_row(snap_id, pid, 1))
    await db_session.flush()

    db_session.add(_consensus_row(snap_id, pid, 2))
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio
async def test_big_board_consensus_rejects_duplicate_rank_per_snapshot(
    db_session: AsyncSession,
    snapshot: ConsensusSnapshot,
    two_players: list[PlayerMaster],
) -> None:
    """uq_big_board_consensus_snapshot_rank blocks two players sharing a rank slot."""
    snap_id = snapshot.id
    p1, p2 = two_players[0].id, two_players[1].id
    assert snap_id is not None and p1 is not None and p2 is not None

    db_session.add(_consensus_row(snap_id, p1, 1))
    await db_session.flush()

    db_session.add(_consensus_row(snap_id, p2, 1))
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio
async def test_source_analytics_rejects_duplicate_source_per_snapshot(
    db_session: AsyncSession,
    snapshot: ConsensusSnapshot,
    two_sources: list[NewsSource],
    two_players: list[PlayerMaster],
) -> None:
    """uq_source_analytics_snapshot_source blocks double-counting a source."""
    from app.schemas.boards import Board, BoardStatus

    # Need a big_board row to satisfy the latest_board_id FK.
    board = Board(
        news_source_id=two_sources[0].id,
        news_item_id=None,
        draft_year=2026,
        published_at=_now(),
        size=0,
        status=BoardStatus.APPROVED,
    )
    db_session.add(board)
    await db_session.flush()

    snap_id = snapshot.id
    src_id = two_sources[0].id
    board_id = board.id
    assert snap_id is not None and src_id is not None and board_id is not None

    db_session.add(
        SourceAnalytics(
            snapshot_id=snap_id,
            news_source_id=src_id,
            latest_board_id=board_id,
            avg_deviation=1.0,
            contrarian_score=0.0,
            biggest_outlier_player_id=None,
            outlier_delta=0,
        )
    )
    await db_session.flush()

    db_session.add(
        SourceAnalytics(
            snapshot_id=snap_id,
            news_source_id=src_id,
            latest_board_id=board_id,
            avg_deviation=2.0,
            contrarian_score=1.0,
            biggest_outlier_player_id=None,
            outlier_delta=0,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
