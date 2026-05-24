"""Integration tests for the Phase 2 consensus computation service.

Exercises:
- recompute_consensus happy path with multiple sources and overlapping
  / non-overlapping players
- the MIN_SOURCES floor (players ranked by only 1 source are excluded)
- prev_rank / rank_delta against a prior snapshot
- per-source analytics (avg_deviation, contrarian z-score, biggest outlier)
- tie-breakers (avg → median → high_rank → player_id) give a stable order
- read paths (get_latest_consensus, get_player_rank_history)
- approve_board hook fires a recompute by default
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.boards import Board, BoardEntry, BoardStatus
from app.schemas.consensus import (
    BigBoardConsensus,
    ConsensusSnapshot,
    ConsensusTrigger,
    SourceAnalytics,
)
from app.schemas.news_sources import FeedType, NewsSource
from app.schemas.players_master import PlayerMaster
from app.services import board_service as bb_svc
from app.services import consensus_service as svc
from tests.integration.conftest import make_player


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _make_source(
    db: AsyncSession, name: str, display: str | None = None
) -> NewsSource:
    src = NewsSource(
        name=name,
        display_name=display or name,
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
    published_at: datetime,
    entries: list[tuple[PlayerMaster, int]],  # (player, rank)
) -> Board:
    """Insert a board straight into APPROVED state without running the
    full create→approve flow (which would trigger a consensus recompute
    and complicate test setup)."""
    assert source.id is not None
    board = Board(
        news_source_id=source.id,
        news_item_id=None,
        draft_year=draft_year,
        published_at=published_at,
        size=len(entries),
        status=BoardStatus.APPROVED,
        approved_at=_now(),
    )
    db.add(board)
    await db.flush()
    assert board.id is not None
    for player, rank in entries:
        assert player.id is not None
        db.add(
            BoardEntry(
                board_id=board.id,
                player_id=player.id,
                position=rank,
            )
        )
    await db.flush()
    return board


@pytest_asyncio.fixture()
async def players(db_session: AsyncSession) -> list[PlayerMaster]:
    rows = [
        make_player("Cooper", "Flagg", school="Duke"),
        make_player("Dylan", "Harper", school="Rutgers"),
        make_player("Ace", "Bailey", school="Rutgers"),
        make_player("VJ", "Edgecombe", school="Baylor"),
    ]
    for p in rows:
        db_session.add(p)
    await db_session.flush()
    return rows


@pytest_asyncio.fixture()
async def three_sources(db_session: AsyncSession) -> list[NewsSource]:
    return [
        await _make_source(db_session, f"src-{i}", display=f"Source {i}")
        for i in range(1, 4)
    ]


@pytest.mark.asyncio
async def test_recompute_aggregates_across_sources(
    db_session: AsyncSession,
    players: list[PlayerMaster],
    three_sources: list[NewsSource],
) -> None:
    """Three boards rank the same four players differently; consensus picks the average."""
    # Source 1 ranks 1,2,3,4
    # Source 2 ranks 2,1,4,3
    # Source 3 ranks 1,3,2,4
    p1, p2, p3, p4 = players
    s1, s2, s3 = three_sources
    today = _now()
    await _make_approved_board(
        db_session,
        source=s1,
        draft_year=2026,
        published_at=today - timedelta(days=2),
        entries=[(p1, 1), (p2, 2), (p3, 3), (p4, 4)],
    )
    await _make_approved_board(
        db_session,
        source=s2,
        draft_year=2026,
        published_at=today - timedelta(days=1),
        entries=[(p2, 1), (p1, 2), (p4, 3), (p3, 4)],
    )
    await _make_approved_board(
        db_session,
        source=s3,
        draft_year=2026,
        published_at=today,
        entries=[(p1, 1), (p3, 2), (p2, 3), (p4, 4)],
    )
    await db_session.commit()

    snap = await svc.recompute_consensus(
        db_session, draft_year=2026, trigger=ConsensusTrigger.MANUAL
    )
    await db_session.commit()
    assert snap.num_boards == 3

    consensus = await svc.get_latest_consensus(db_session, draft_year=2026)
    assert [c.player_id for c in consensus] == [p1.id, p2.id, p3.id, p4.id]
    assert all(c.num_sources == 3 for c in consensus)

    by_player = {c.player_id: c for c in consensus}
    assert p1.id is not None
    p1_row = by_player[p1.id]
    assert pytest.approx(p1_row.avg_rank) == 4 / 3  # (1+2+1)/3
    assert p1_row.high_rank == 1
    assert p1_row.low_rank == 2


@pytest.mark.asyncio
async def test_min_sources_floor_excludes_solo_picks(
    db_session: AsyncSession,
    players: list[PlayerMaster],
    three_sources: list[NewsSource],
) -> None:
    """A player ranked by only one source is excluded from the consensus output."""
    p1, p2, p3, _p4 = players
    s1, s2, _s3 = three_sources
    today = _now()
    # Two sources cover p1 + p2; only s1 ranks p3.
    await _make_approved_board(
        db_session,
        source=s1,
        draft_year=2026,
        published_at=today - timedelta(days=1),
        entries=[(p1, 1), (p2, 2), (p3, 3)],
    )
    await _make_approved_board(
        db_session,
        source=s2,
        draft_year=2026,
        published_at=today,
        entries=[(p1, 1), (p2, 2)],
    )
    await db_session.commit()

    await svc.recompute_consensus(
        db_session, draft_year=2026, trigger=ConsensusTrigger.MANUAL
    )
    await db_session.commit()

    consensus = await svc.get_latest_consensus(db_session, draft_year=2026)
    assert {c.player_id for c in consensus} == {p1.id, p2.id}
    # p3 was on only one board so didn't clear MIN_SOURCES=2.


@pytest.mark.asyncio
async def test_prev_rank_and_rank_delta_from_prior_snapshot(
    db_session: AsyncSession,
    players: list[PlayerMaster],
    three_sources: list[NewsSource],
) -> None:
    """A second recompute populates prev_rank and rank_delta from the first."""
    p1, p2, _p3, _p4 = players
    s1, s2, _s3 = three_sources
    today = _now()

    # First state: s1 + s2 both rank p1 #1, p2 #2.
    board_s1 = await _make_approved_board(
        db_session,
        source=s1,
        draft_year=2026,
        published_at=today - timedelta(days=5),
        entries=[(p1, 1), (p2, 2)],
    )
    await _make_approved_board(
        db_session,
        source=s2,
        draft_year=2026,
        published_at=today - timedelta(days=4),
        entries=[(p1, 1), (p2, 2)],
    )
    await db_session.commit()
    snap1 = await svc.recompute_consensus(
        db_session, draft_year=2026, trigger=ConsensusTrigger.MANUAL
    )
    await db_session.commit()
    snap1_id = snap1.id

    # s1 publishes a new board flipping p1 and p2.
    board_s1.id  # touch
    await _make_approved_board(
        db_session,
        source=s1,
        draft_year=2026,
        published_at=today,  # newer than the first s1 board
        entries=[(p2, 1), (p1, 2)],
    )
    await db_session.commit()
    snap2 = await svc.recompute_consensus(
        db_session, draft_year=2026, trigger=ConsensusTrigger.MANUAL
    )
    await db_session.commit()
    assert snap2.id != snap1_id

    consensus = await svc.get_latest_consensus(db_session, draft_year=2026)
    by_player = {c.player_id: c for c in consensus}
    assert p1.id is not None and p2.id is not None
    p1_id, p2_id = p1.id, p2.id

    # After s1 flips, the avg ranks tie at 1.5 each; tie-breaker
    # (median → high_rank → player_id) gives the lower player_id the top slot.
    expected_top = min(p1_id, p2_id)
    assert by_player[expected_top].consensus_rank == 1

    # prev_rank should come from snap1, where p1 was #1 and p2 was #2.
    assert by_player[p1_id].prev_rank == 1
    assert by_player[p2_id].prev_rank == 2
    # rank_delta = prev_rank - consensus_rank (positive = rising).
    for pid in (p1_id, p2_id):
        c = by_player[pid]
        assert c.prev_rank is not None
        assert c.rank_delta == (c.prev_rank - c.consensus_rank)


@pytest.mark.asyncio
async def test_source_analytics_computes_deviation_and_contrarian_score(
    db_session: AsyncSession,
    players: list[PlayerMaster],
    three_sources: list[NewsSource],
) -> None:
    """Each source gets one analytics row per snapshot; z-scores are normalized across the snapshot."""
    p1, p2, p3, p4 = players
    s1, s2, s3 = three_sources
    today = _now()

    # s1 + s3 agree closely; s2 is the contrarian (flips top 2).
    await _make_approved_board(
        db_session,
        source=s1,
        draft_year=2026,
        published_at=today - timedelta(days=2),
        entries=[(p1, 1), (p2, 2), (p3, 3), (p4, 4)],
    )
    await _make_approved_board(
        db_session,
        source=s2,
        draft_year=2026,
        published_at=today - timedelta(days=1),
        entries=[(p4, 1), (p3, 2), (p2, 3), (p1, 4)],
    )
    await _make_approved_board(
        db_session,
        source=s3,
        draft_year=2026,
        published_at=today,
        entries=[(p1, 1), (p2, 2), (p3, 3), (p4, 4)],
    )
    await db_session.commit()
    snap = await svc.recompute_consensus(
        db_session, draft_year=2026, trigger=ConsensusTrigger.MANUAL
    )
    await db_session.commit()

    rows = (
        await db_session.execute(
            select(SourceAnalytics).where(SourceAnalytics.snapshot_id == snap.id)  # type: ignore[arg-type]
        )
    ).scalars().all()
    assert len(rows) == 3
    by_source = {r.news_source_id: r for r in rows}
    assert s1.id is not None and s2.id is not None and s3.id is not None
    s1_id, s2_id, s3_id = s1.id, s2.id, s3.id

    # s2 should have the highest avg_deviation (it flipped the order).
    s2_dev = by_source[s2_id].avg_deviation
    other_devs = [by_source[s1_id].avg_deviation, by_source[s3_id].avg_deviation]
    assert s2_dev > max(other_devs)
    # And the highest contrarian score (positive z-score).
    assert by_source[s2_id].contrarian_score > 0
    assert by_source[s1_id].contrarian_score < 0


@pytest.mark.asyncio
async def test_single_source_still_gets_source_analytics_row(
    db_session: AsyncSession,
    players: list[PlayerMaster],
    three_sources: list[NewsSource],
) -> None:
    """With only one approved board, no player clears MIN_SOURCES=2 — but
    the eligible source still gets a SourceAnalytics row so the
    per-source-per-snapshot invariant holds."""
    p1, p2, _p3, _p4 = players
    s1, _s2, _s3 = three_sources
    await _make_approved_board(
        db_session,
        source=s1,
        draft_year=2026,
        published_at=_now(),
        entries=[(p1, 1), (p2, 2)],
    )
    await db_session.commit()

    snap = await svc.recompute_consensus(
        db_session, draft_year=2026, trigger=ConsensusTrigger.MANUAL
    )
    await db_session.commit()
    assert snap.num_boards == 1

    # Consensus is empty (MIN_SOURCES=2 not met).
    consensus = await svc.get_latest_consensus(db_session, draft_year=2026)
    assert consensus == []

    # But the SourceAnalytics row still exists.
    rows = (
        await db_session.execute(
            select(SourceAnalytics).where(SourceAnalytics.snapshot_id == snap.id)  # type: ignore[arg-type]
        )
    ).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.news_source_id == s1.id
    assert row.avg_deviation == 0.0
    assert row.contrarian_score == 0.0
    assert row.biggest_outlier_player_id is None


@pytest.mark.asyncio
async def test_eligible_board_selection_is_deterministic_with_id_tiebreak(
    db_session: AsyncSession,
    players: list[PlayerMaster],
    three_sources: list[NewsSource],
) -> None:
    """When two boards from one source share published_at, the higher-id
    (later-inserted) board wins reproducibly. Without the id tie-break a
    re-run could pick either one and produce different consensus."""
    p1, p2, _p3, _p4 = players
    s1, s2, _s3 = three_sources
    same_dt = _now()

    older = await _make_approved_board(
        db_session,
        source=s1,
        draft_year=2026,
        published_at=same_dt,
        entries=[(p1, 1), (p2, 2)],
    )
    newer = await _make_approved_board(
        db_session,
        source=s1,
        draft_year=2026,
        published_at=same_dt,
        entries=[(p2, 1), (p1, 2)],  # flipped order
    )
    # Second source so MIN_SOURCES=2 lets the players appear on consensus.
    await _make_approved_board(
        db_session,
        source=s2,
        draft_year=2026,
        published_at=same_dt,
        entries=[(p1, 1), (p2, 2)],
    )
    await db_session.commit()
    assert older.id is not None and newer.id is not None
    assert newer.id > older.id

    # Recompute twice; both should pick the higher-id board.
    snap_a = await svc.recompute_consensus(
        db_session, draft_year=2026, trigger=ConsensusTrigger.MANUAL
    )
    await db_session.commit()
    snap_b = await svc.recompute_consensus(
        db_session, draft_year=2026, trigger=ConsensusTrigger.MANUAL
    )
    await db_session.commit()

    assert newer.id in snap_a.board_ids
    assert older.id not in snap_a.board_ids
    assert newer.id in snap_b.board_ids
    assert older.id not in snap_b.board_ids
    # Same eligible board set across runs.
    assert sorted(snap_a.board_ids) == sorted(snap_b.board_ids)


@pytest.mark.asyncio
async def test_empty_year_writes_snapshot_with_zero_boards(
    db_session: AsyncSession,
) -> None:
    """No APPROVED boards yet → still get a snapshot row (audit marker)."""
    snap = await svc.recompute_consensus(
        db_session, draft_year=2099, trigger=ConsensusTrigger.MANUAL
    )
    await db_session.commit()
    assert snap.num_boards == 0
    assert snap.board_ids == []
    consensus = await svc.get_latest_consensus(db_session, draft_year=2099)
    assert consensus == []


@pytest.mark.asyncio
async def test_player_rank_history_returns_oldest_first(
    db_session: AsyncSession,
    players: list[PlayerMaster],
    three_sources: list[NewsSource],
) -> None:
    """get_player_rank_history orders snapshots chronologically."""
    p1, p2, _p3, _p4 = players
    s1, s2, _s3 = three_sources
    today = _now()
    await _make_approved_board(
        db_session,
        source=s1,
        draft_year=2026,
        published_at=today - timedelta(days=10),
        entries=[(p1, 1), (p2, 2)],
    )
    await _make_approved_board(
        db_session,
        source=s2,
        draft_year=2026,
        published_at=today - timedelta(days=9),
        entries=[(p1, 1), (p2, 2)],
    )
    await db_session.commit()
    await svc.recompute_consensus(
        db_session, draft_year=2026, trigger=ConsensusTrigger.MANUAL
    )
    await db_session.commit()
    await svc.recompute_consensus(
        db_session, draft_year=2026, trigger=ConsensusTrigger.SCHEDULED
    )
    await db_session.commit()

    assert p1.id is not None
    history = await svc.get_player_rank_history(
        db_session, player_id=p1.id, draft_year=2026
    )
    assert len(history) == 2
    # snap row.consensus_rank should be 1 across both passes.
    assert all(c.consensus_rank == 1 for c in history)


@pytest.mark.asyncio
async def test_approve_board_triggers_recompute_by_default(
    db_session: AsyncSession,
    players: list[PlayerMaster],
    three_sources: list[NewsSource],
) -> None:
    """approve_board writes a fresh ConsensusSnapshot unless explicitly disabled."""
    p1, p2, _p3, _p4 = players
    s1, s2, _s3 = three_sources
    today = _now()

    # Seed two boards. We'll approve the second through the service so
    # the hook can fire on its trigger.
    await _make_approved_board(
        db_session,
        source=s1,
        draft_year=2026,
        published_at=today - timedelta(days=1),
        entries=[(p1, 1), (p2, 2)],
    )

    pending = Board(
        news_source_id=s2.id,
        news_item_id=None,
        draft_year=2026,
        published_at=today,
        size=2,
        status=BoardStatus.PENDING,
    )
    db_session.add(pending)
    await db_session.flush()
    assert pending.id is not None
    db_session.add(BoardEntry(board_id=pending.id, player_id=p1.id, position=1))
    db_session.add(BoardEntry(board_id=pending.id, player_id=p2.id, position=2))
    await db_session.commit()

    snapshots_before = (
        await db_session.execute(select(ConsensusSnapshot))
    ).scalars().all()
    assert snapshots_before == []

    await bb_svc.approve_board(db_session, board_id=pending.id)
    await db_session.commit()

    snapshots_after = (
        await db_session.execute(select(ConsensusSnapshot))
    ).scalars().all()
    assert len(snapshots_after) == 1
    assert snapshots_after[0].trigger is ConsensusTrigger.BOARD_APPROVED
    assert snapshots_after[0].num_boards == 2

    # And the consensus rows actually got written.
    rows = (
        await db_session.execute(
            select(BigBoardConsensus).where(
                BigBoardConsensus.snapshot_id == snapshots_after[0].id  # type: ignore[arg-type]
            )
        )
    ).scalars().all()
    assert {r.player_id for r in rows} == {p1.id, p2.id}


@pytest.mark.asyncio
async def test_approve_board_can_skip_recompute(
    db_session: AsyncSession,
    players: list[PlayerMaster],
    three_sources: list[NewsSource],
) -> None:
    """approve_board(recompute_consensus=False) is the escape hatch for tests / batch ops."""
    p1, _p2, _p3, _p4 = players
    s1, _s2, _s3 = three_sources
    pending = Board(
        news_source_id=s1.id,
        news_item_id=None,
        draft_year=2026,
        published_at=_now(),
        size=1,
        status=BoardStatus.PENDING,
    )
    db_session.add(pending)
    await db_session.flush()
    assert pending.id is not None
    db_session.add(BoardEntry(board_id=pending.id, player_id=p1.id, position=1))
    await db_session.commit()

    await bb_svc.approve_board(
        db_session, board_id=pending.id, recompute_consensus=False
    )
    await db_session.commit()
    snapshots = (
        await db_session.execute(select(ConsensusSnapshot))
    ).scalars().all()
    assert snapshots == []
