"""Thin read-layer for the public consensus API.

Shapes data from ``BigBoardConsensus``, ``SourceAnalytics``, and
``ConsensusSnapshot`` into the Pydantic response models consumed by
``app/routes/consensus.py``.

The write/compute path lives in ``consensus_service.py``; this module
only reads. UI tickets #218–#221 will extend the helpers here.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.consensus import (
    ConsensusRow,
    PlayerConsensusDetail,
    RankHistoryPoint,
    SnapshotSummary,
    SourceAnalyticsRow,
    SourceRankEntry,
)
from app.schemas.boards import Board, BoardEntry
from app.schemas.consensus import (
    BigBoardConsensus,
    ConsensusSnapshot,
    SourceAnalytics,
)
from app.schemas.news_sources import NewsSource
from app.schemas.players_master import PlayerMaster


async def _resolve_snapshot_id(
    db: AsyncSession,
    *,
    draft_year: int,
    snapshot_id: Optional[int],
) -> Optional[int]:
    """Return the target snapshot id.

    When ``snapshot_id`` is supplied it is returned as-is (caller is
    responsible for validating it exists). When omitted, the most recent
    snapshot for ``draft_year`` is selected.
    """
    if snapshot_id is not None:
        return snapshot_id
    sid = await db.scalar(
        select(ConsensusSnapshot.id)  # type: ignore[call-overload]
        .where(ConsensusSnapshot.draft_year == draft_year)  # type: ignore[arg-type]
        .order_by(ConsensusSnapshot.computed_at.desc())  # type: ignore[attr-defined]
        .limit(1)
    )
    return sid  # type: ignore[return-value]


async def _player_name_map(
    db: AsyncSession, player_ids: list[int]
) -> dict[int, PlayerMaster]:
    """Return a ``player_id -> PlayerMaster`` map for a batch of ids."""
    if not player_ids:
        return {}
    rows = (
        (
            await db.execute(
                select(PlayerMaster).where(  # type: ignore[call-overload]
                    PlayerMaster.id.in_(player_ids)  # type: ignore[union-attr]
                )
            )
        )
        .scalars()
        .all()
    )
    return {p.id: p for p in rows if p.id is not None}


def _to_consensus_row(
    bbc: BigBoardConsensus, player: Optional[PlayerMaster]
) -> ConsensusRow:
    """Map a ``BigBoardConsensus`` ORM row to the ``ConsensusRow`` model."""
    return ConsensusRow(
        player_id=bbc.player_id,
        player_name=player.display_name if player else None,
        school=player.school if player else None,
        slug=player.slug if player else None,
        consensus_rank=bbc.consensus_rank,
        avg_rank=bbc.avg_rank,
        median_rank=bbc.median_rank,
        high_rank=bbc.high_rank,
        low_rank=bbc.low_rank,
        std_dev=bbc.std_dev,
        num_sources=bbc.num_sources,
        prev_rank=bbc.prev_rank,
        rank_delta=bbc.rank_delta,
    )


async def get_consensus_board(
    db: AsyncSession,
    *,
    draft_year: int,
    snapshot_id: Optional[int] = None,
) -> list[ConsensusRow]:
    """Return ordered consensus rows for a draft year.

    Args:
        db: Async DB session.
        draft_year: The draft class to query.
        snapshot_id: Specific snapshot; defaults to the most recent.

    Returns:
        Rows ordered by ``consensus_rank`` asc. Empty list when no
        snapshot exists for the year.
    """
    sid = await _resolve_snapshot_id(db, draft_year=draft_year, snapshot_id=snapshot_id)
    if sid is None:
        return []

    bbc_rows = (
        (
            await db.execute(
                select(BigBoardConsensus)  # type: ignore[call-overload]
                .where(BigBoardConsensus.snapshot_id == sid)  # type: ignore[arg-type]
                .order_by(BigBoardConsensus.consensus_rank)  # type: ignore[arg-type]
            )
        )
        .scalars()
        .all()
    )

    if not bbc_rows:
        return []

    player_map = await _player_name_map(db, [r.player_id for r in bbc_rows])
    return [_to_consensus_row(r, player_map.get(r.player_id)) for r in bbc_rows]


async def get_player_consensus_detail(
    db: AsyncSession,
    *,
    player_id: int,
    draft_year: int,
) -> Optional[PlayerConsensusDetail]:
    """Return full consensus detail for one player.

    Args:
        db: Async DB session.
        player_id: The player to look up.
        draft_year: Draft class to query.

    Returns:
        ``PlayerConsensusDetail`` when a current consensus row exists,
        ``None`` otherwise (caller should raise 404).
    """
    # Current snapshot row.
    sid = await _resolve_snapshot_id(db, draft_year=draft_year, snapshot_id=None)
    if sid is None:
        return None

    bbc = (
        await db.execute(
            select(BigBoardConsensus)  # type: ignore[call-overload]
            .where(BigBoardConsensus.snapshot_id == sid)  # type: ignore[arg-type]
            .where(BigBoardConsensus.player_id == player_id)  # type: ignore[arg-type]
        )
    ).scalar_one_or_none()

    if bbc is None:
        return None

    # Player metadata.
    player = (
        await db.execute(
            select(PlayerMaster).where(  # type: ignore[call-overload]
                PlayerMaster.id == player_id  # type: ignore[arg-type]
            )
        )
    ).scalar_one_or_none()

    # Per-source breakdown.
    # Join board_entries → boards → news_sources for the boards that fed
    # the latest snapshot.
    snapshot = (
        await db.execute(
            select(ConsensusSnapshot).where(  # type: ignore[call-overload]
                ConsensusSnapshot.id == sid  # type: ignore[arg-type]
            )
        )
    ).scalar_one_or_none()

    source_ranks: list[SourceRankEntry] = []
    if snapshot and snapshot.board_ids:
        entry_rows = (
            await db.execute(
                select(BoardEntry.board_id, BoardEntry.position)  # type: ignore[call-overload]
                .where(BoardEntry.board_id.in_(snapshot.board_ids))  # type: ignore[union-attr, attr-defined]
                .where(BoardEntry.player_id == player_id)  # type: ignore[arg-type]
            )
        ).all()

        if entry_rows:
            bid_to_rank = {row.board_id: row.position for row in entry_rows}
            board_rows = (
                (
                    await db.execute(
                        select(Board).where(  # type: ignore[call-overload]
                            Board.id.in_(list(bid_to_rank.keys()))  # type: ignore[union-attr]
                        )
                    )
                )
                .scalars()
                .all()
            )

            source_ids = [b.news_source_id for b in board_rows]
            source_rows = (
                (
                    await db.execute(
                        select(NewsSource).where(  # type: ignore[call-overload]
                            NewsSource.id.in_(source_ids)  # type: ignore[union-attr]
                        )
                    )
                )
                .scalars()
                .all()
            )
            source_map = {s.id: s for s in source_rows if s.id is not None}

            for board in board_rows:
                if board.id is None:
                    continue
                src = source_map.get(board.news_source_id)
                source_ranks.append(
                    SourceRankEntry(
                        news_source_id=board.news_source_id,
                        source_name=src.name
                        if src
                        else f"source_{board.news_source_id}",
                        source_display_name=src.display_name
                        if src
                        else f"source_{board.news_source_id}",
                        source_rank=bid_to_rank[board.id],
                    )
                )
        source_ranks.sort(key=lambda e: e.source_rank)

    # Rank history (oldest → newest).
    history_bbc_rows = (
        await db.execute(
            select(BigBoardConsensus, ConsensusSnapshot.computed_at)  # type: ignore[call-overload]
            .join(
                ConsensusSnapshot,
                ConsensusSnapshot.id == BigBoardConsensus.snapshot_id,  # type: ignore[arg-type]
            )
            .where(BigBoardConsensus.player_id == player_id)  # type: ignore[arg-type]
            .where(BigBoardConsensus.draft_year == draft_year)  # type: ignore[arg-type]
            .order_by(ConsensusSnapshot.computed_at)  # type: ignore[arg-type]
        )
    ).all()

    rank_history = [
        RankHistoryPoint(
            computed_at=row.computed_at,
            consensus_rank=row.BigBoardConsensus.consensus_rank,
            snapshot_id=row.BigBoardConsensus.snapshot_id,
        )
        for row in history_bbc_rows
    ]

    return PlayerConsensusDetail(
        player_id=player_id,
        player_name=player.display_name if player else None,
        school=player.school if player else None,
        consensus_rank=bbc.consensus_rank,
        avg_rank=bbc.avg_rank,
        median_rank=bbc.median_rank,
        high_rank=bbc.high_rank,
        low_rank=bbc.low_rank,
        std_dev=bbc.std_dev,
        num_sources=bbc.num_sources,
        prev_rank=bbc.prev_rank,
        rank_delta=bbc.rank_delta,
        source_ranks=source_ranks,
        rank_history=rank_history,
    )


async def get_source_analytics(
    db: AsyncSession,
    *,
    draft_year: int,
) -> list[SourceAnalyticsRow]:
    """Return source analytics rows for the latest snapshot of a draft year.

    Args:
        db: Async DB session.
        draft_year: Draft class to query.

    Returns:
        One row per source in the latest snapshot, ordered by
        ``contrarian_score`` desc. Empty list when no snapshot exists.
    """
    sid = await _resolve_snapshot_id(db, draft_year=draft_year, snapshot_id=None)
    if sid is None:
        return []

    sa_rows = (
        (
            await db.execute(
                select(SourceAnalytics)  # type: ignore[call-overload]
                .where(SourceAnalytics.snapshot_id == sid)  # type: ignore[arg-type]
                .order_by(SourceAnalytics.contrarian_score.desc())  # type: ignore[attr-defined]
            )
        )
        .scalars()
        .all()
    )

    if not sa_rows:
        return []

    source_ids = [r.news_source_id for r in sa_rows]
    source_rows = (
        (
            await db.execute(
                select(NewsSource).where(  # type: ignore[call-overload]
                    NewsSource.id.in_(source_ids)  # type: ignore[union-attr]
                )
            )
        )
        .scalars()
        .all()
    )
    source_map = {s.id: s for s in source_rows if s.id is not None}

    out: list[SourceAnalyticsRow] = []
    for row in sa_rows:
        src = source_map.get(row.news_source_id)
        assert row.id is not None
        out.append(
            SourceAnalyticsRow(
                id=row.id,
                snapshot_id=row.snapshot_id,
                news_source_id=row.news_source_id,
                source_name=src.name if src else f"source_{row.news_source_id}",
                source_display_name=src.display_name
                if src
                else f"source_{row.news_source_id}",
                latest_board_id=row.latest_board_id,
                avg_deviation=row.avg_deviation,
                contrarian_score=row.contrarian_score,
                biggest_outlier_player_id=row.biggest_outlier_player_id,
                outlier_delta=row.outlier_delta,
            )
        )
    return out


async def get_snapshots(
    db: AsyncSession,
    *,
    draft_year: int,
    limit: int = 10,
) -> list[SnapshotSummary]:
    """Return recent snapshots for a draft year, newest first.

    Args:
        db: Async DB session.
        draft_year: Draft class to query.
        limit: Maximum number of snapshots to return.

    Returns:
        Snapshots ordered by ``computed_at`` desc, up to ``limit``.
    """
    rows = (
        (
            await db.execute(
                select(ConsensusSnapshot)  # type: ignore[call-overload]
                .where(ConsensusSnapshot.draft_year == draft_year)  # type: ignore[arg-type]
                .order_by(ConsensusSnapshot.computed_at.desc())  # type: ignore[attr-defined]
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    return [
        SnapshotSummary(
            id=row.id,  # type: ignore[arg-type]
            draft_year=row.draft_year,
            computed_at=row.computed_at,
            num_boards=row.num_boards,
            trigger=row.trigger,
        )
        for row in rows
    ]
