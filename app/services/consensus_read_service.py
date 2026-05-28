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
from app.schemas.boards import Board, BoardEntry, BoardStatus
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
    """Return the target snapshot id, scoped to ``draft_year``.

    When ``snapshot_id`` is supplied it is validated against ``draft_year``
    in the same query — a snapshot belonging to a different year returns
    ``None`` so callers behave identically to "no data" rather than
    surfacing cross-year rows under the requested year. When ``snapshot_id``
    is omitted, the most recent snapshot for ``draft_year`` is selected.
    """
    if snapshot_id is not None:
        return await db.scalar(
            select(ConsensusSnapshot.id)  # type: ignore[call-overload]
            .where(ConsensusSnapshot.id == snapshot_id)  # type: ignore[arg-type]
            .where(ConsensusSnapshot.draft_year == draft_year)  # type: ignore[arg-type]
        )
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


async def get_source_leaderboard(
    db: AsyncSession,
    *,
    draft_year: int,
) -> list[dict]:
    """Return source analytics rows shaped for the /sources leaderboard page.

    Each dict includes source name, slug, contrarian score, avg deviation,
    and biggest-outlier player info. Ordered by contrarian_score desc.

    Args:
        db: Async DB session.
        draft_year: Draft class to query.

    Returns:
        List of dicts ready for template rendering, or empty list when no
        snapshot exists for ``draft_year``.
    """
    from app.utils.slug import generate_slug

    analytics_rows = await get_source_analytics(db, draft_year=draft_year)
    if not analytics_rows:
        return []

    outlier_player_ids = [
        r.biggest_outlier_player_id
        for r in analytics_rows
        if r.biggest_outlier_player_id is not None
    ]
    outlier_player_map = await _player_name_map(db, outlier_player_ids)

    out: list[dict] = []
    for row in analytics_rows:
        outlier_player = (
            outlier_player_map.get(row.biggest_outlier_player_id)
            if row.biggest_outlier_player_id is not None
            else None
        )
        out.append(
            {
                "news_source_id": row.news_source_id,
                "source_name": row.source_name,
                "source_display_name": row.source_display_name,
                "source_slug": generate_slug(row.source_name),
                "contrarian_score": row.contrarian_score,
                "avg_deviation": row.avg_deviation,
                "biggest_outlier_player_name": outlier_player.display_name
                if outlier_player
                else None,
                "biggest_outlier_player_slug": outlier_player.slug
                if outlier_player
                else None,
                "outlier_delta": row.outlier_delta,
            }
        )
    return out


async def get_source_detail(
    db: AsyncSession,
    *,
    source_slug: str,
    draft_year: int,
) -> Optional[dict]:
    """Return detail data for a single source: its board vs consensus overlay.

    Resolves the source by slug-matching ``NewsSource.name`` (kebab-cased).
    Returns ``None`` when no source matches the slug.

    Args:
        db: Async DB session.
        source_slug: URL slug derived from ``NewsSource.name``.
        draft_year: Draft class to query.

    Returns:
        Dict with source metadata, per-player overlay rows (source rank vs
        consensus rank), and analytics summary; or ``None`` when the slug
        does not match any known source.
    """
    from app.utils.slug import generate_slug

    # --- Resolve source by slug -----------------------------------------------
    all_sources = (
        (
            await db.execute(
                select(NewsSource)  # type: ignore[call-overload]
            )
        )
        .scalars()
        .all()
    )
    matched_source: Optional[NewsSource] = None
    for src in all_sources:
        if generate_slug(src.name) == source_slug:
            matched_source = src
            break
    if matched_source is None or matched_source.id is None:
        return None

    # --- Source analytics row for this source ---------------------------------
    sid = await _resolve_snapshot_id(db, draft_year=draft_year, snapshot_id=None)
    if sid is None:
        return None

    sa_row = (
        await db.execute(
            select(SourceAnalytics)  # type: ignore[call-overload]
            .where(SourceAnalytics.snapshot_id == sid)  # type: ignore[arg-type]
            .where(SourceAnalytics.news_source_id == matched_source.id)  # type: ignore[arg-type]
        )
    ).scalar_one_or_none()

    if sa_row is None:
        return None

    # --- Source board entries (latest approved board for this source) ----------
    source_board = (
        await db.execute(
            select(Board).where(Board.id == sa_row.latest_board_id)  # type: ignore[call-overload, arg-type]
        )
    ).scalar_one_or_none()

    source_entries: list[BoardEntry] = []
    if source_board is not None and source_board.id is not None:
        source_entries = list(
            (
                await db.execute(
                    select(BoardEntry)  # type: ignore[call-overload]
                    .where(BoardEntry.board_id == source_board.id)  # type: ignore[arg-type]
                    .order_by(BoardEntry.position)  # type: ignore[arg-type]
                )
            )
            .scalars()
            .all()
        )

    # --- Consensus board for overlay ------------------------------------------
    consensus_rows = await get_consensus_board(
        db, draft_year=draft_year, snapshot_id=sid
    )
    consensus_rank_map = {r.player_id: r for r in consensus_rows}

    # --- Build overlay rows ---------------------------------------------------
    all_player_ids = [e.player_id for e in source_entries if e.player_id is not None]
    player_map = await _player_name_map(db, all_player_ids)

    # Biggest outlier player id (for highlighting)
    biggest_outlier_player_id = sa_row.biggest_outlier_player_id

    overlay_rows: list[dict] = []
    for entry in source_entries:
        pid = entry.player_id
        player = player_map.get(pid) if pid is not None else None  # type: ignore[arg-type]
        consensus_row = consensus_rank_map.get(pid) if pid is not None else None  # type: ignore[arg-type]
        delta = None
        if consensus_row is not None:
            delta = (
                entry.position - consensus_row.consensus_rank
            )  # positive = source lower
        overlay_rows.append(
            {
                "player_id": entry.player_id,
                "player_name": player.display_name if player else None,
                "player_slug": player.slug if player else None,
                "source_rank": entry.position,
                "consensus_rank": consensus_row.consensus_rank
                if consensus_row
                else None,
                "delta": delta,
                "is_biggest_outlier": entry.player_id == biggest_outlier_player_id,
            }
        )

    return {
        "news_source_id": matched_source.id,
        "source_name": matched_source.name,
        "source_display_name": matched_source.display_name,
        "source_slug": source_slug,
        "avg_deviation": sa_row.avg_deviation,
        "contrarian_score": sa_row.contrarian_score,
        "outlier_delta": sa_row.outlier_delta,
        "biggest_outlier_player_id": biggest_outlier_player_id,
        "overlay_rows": overlay_rows,
        "draft_year": draft_year,
    }


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


async def get_biggest_movers(
    db: AsyncSession,
    *,
    draft_year: int,
    k: int = 5,
) -> dict:
    """Return the top risers and fallers between the two most recent snapshots.

    Compares ``rank_delta`` (positive = rising, negative = falling) on the
    most recent snapshot. Rows with no ``rank_delta`` (i.e. only one snapshot
    exists) are excluded, so the result may be empty.

    Args:
        db: Async DB session.
        draft_year: Draft class to query.
        k: Maximum number of risers and fallers to return (each).

    Returns:
        ``{"risers": [...], "fallers": [...]}`` where each item is a dict with
        ``player_id``, ``player_name``, ``slug``, ``consensus_rank``,
        ``rank_delta``, and ``prev_rank``. Both lists are empty when no prior
        snapshot exists.
    """
    sid = await _resolve_snapshot_id(db, draft_year=draft_year, snapshot_id=None)
    if sid is None:
        return {"risers": [], "fallers": []}

    # Fetch all rows for the current snapshot that have a non-null rank_delta.
    bbc_rows = (
        (
            await db.execute(
                select(BigBoardConsensus)  # type: ignore[call-overload]
                .where(BigBoardConsensus.snapshot_id == sid)  # type: ignore[arg-type]
                .where(BigBoardConsensus.rank_delta.is_not(None))  # type: ignore[union-attr]
            )
        )
        .scalars()
        .all()
    )

    if not bbc_rows:
        return {"risers": [], "fallers": []}

    player_ids = [r.player_id for r in bbc_rows]
    player_map = await _player_name_map(db, player_ids)

    def _to_mover(bbc: BigBoardConsensus) -> dict:
        player = player_map.get(bbc.player_id)
        return {
            "player_id": bbc.player_id,
            "player_name": player.display_name if player else None,
            "slug": player.slug if player else None,
            "consensus_rank": bbc.consensus_rank,
            "rank_delta": bbc.rank_delta,
            "prev_rank": bbc.prev_rank,
        }

    # rank_delta > 0 → risen (smaller rank number); sort descending by delta
    risers = sorted(
        [r for r in bbc_rows if r.rank_delta is not None and r.rank_delta > 0],
        key=lambda r: -(r.rank_delta or 0),
    )[:k]

    # rank_delta < 0 → fallen; sort ascending (most negative first)
    fallers = sorted(
        [r for r in bbc_rows if r.rank_delta is not None and r.rank_delta < 0],
        key=lambda r: (r.rank_delta or 0),
    )[:k]

    return {
        "risers": [_to_mover(r) for r in risers],
        "fallers": [_to_mover(r) for r in fallers],
    }


async def get_source_spotlight(
    db: AsyncSession,
    *,
    draft_year: int,
) -> Optional[dict]:
    """Return the most contrarian source for the latest snapshot.

    Selects the ``SourceAnalytics`` row with the highest ``contrarian_score``
    and returns a callout-ready dict.

    Args:
        db: Async DB session.
        draft_year: Draft class to query.

    Returns:
        Dict with ``source_name``, ``source_display_name``,
        ``avg_deviation``, and ``contrarian_score``, or ``None`` when no
        source analytics exist for the latest snapshot.
    """
    sid = await _resolve_snapshot_id(db, draft_year=draft_year, snapshot_id=None)
    if sid is None:
        return None

    sa_row = (
        await db.execute(
            select(SourceAnalytics)  # type: ignore[call-overload]
            .where(SourceAnalytics.snapshot_id == sid)  # type: ignore[arg-type]
            .order_by(SourceAnalytics.contrarian_score.desc())  # type: ignore[attr-defined]
            .limit(1)
        )
    ).scalar_one_or_none()

    if sa_row is None:
        return None

    src = (
        await db.execute(
            select(NewsSource).where(  # type: ignore[call-overload]
                NewsSource.id == sa_row.news_source_id  # type: ignore[arg-type]
            )
        )
    ).scalar_one_or_none()

    return {
        "news_source_id": sa_row.news_source_id,
        "source_name": src.name if src else f"source_{sa_row.news_source_id}",
        "source_display_name": src.display_name
        if src
        else f"source_{sa_row.news_source_id}",
        "avg_deviation": sa_row.avg_deviation,
        "contrarian_score": sa_row.contrarian_score,
    }


async def get_board_freshness(
    db: AsyncSession,
    *,
    draft_year: int,
) -> Optional[dict]:
    """Return freshness metadata for the latest snapshot.

    Derives board count, unique source count, and the latest ``published_at``
    date from the APPROVED boards that fed the current snapshot.

    Args:
        db: Async DB session.
        draft_year: Draft class to query.

    Returns:
        Dict with ``num_boards``, ``num_sources``, and ``last_updated``
        (a ``datetime``), or ``None`` when no snapshot exists.
    """
    sid = await _resolve_snapshot_id(db, draft_year=draft_year, snapshot_id=None)
    if sid is None:
        return None

    snapshot = (
        await db.execute(
            select(ConsensusSnapshot).where(  # type: ignore[call-overload]
                ConsensusSnapshot.id == sid  # type: ignore[arg-type]
            )
        )
    ).scalar_one_or_none()

    if snapshot is None or not snapshot.board_ids:
        return None

    board_rows = (
        (
            await db.execute(
                select(Board)  # type: ignore[call-overload]
                .where(Board.id.in_(snapshot.board_ids))  # type: ignore[union-attr]
                .where(Board.status == BoardStatus.APPROVED)  # type: ignore[arg-type]
            )
        )
        .scalars()
        .all()
    )

    if not board_rows:
        return None

    unique_sources = len({b.news_source_id for b in board_rows})
    last_updated = max(
        (b.approved_at for b in board_rows if b.approved_at is not None),
        default=snapshot.computed_at,
    )

    return {
        "num_boards": len(board_rows),
        "num_sources": unique_sources,
        "last_updated": last_updated,
    }
