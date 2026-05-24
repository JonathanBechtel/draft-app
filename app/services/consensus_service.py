"""Phase 2 consensus computation service.

Aggregates the most recent APPROVED big board per source into a single
ranked consensus board for a draft year, plus per-source deviation
analytics. Append-only snapshots so rank trajectory falls out of a
simple query in Phase 3.

See ``docs/consensus_phase_2_design.md`` for the algorithm rationale.
Tier values stored on big_board_entries are intentionally ignored in
the aggregation — tier is admin-side transcription fidelity, not a
consensus signal in v1.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.big_boards import BigBoard, BigBoardEntry, BoardStatus
from app.schemas.consensus import (
    BigBoardConsensus,
    ConsensusSnapshot,
    ConsensusTrigger,
    SourceAnalytics,
)


# Players ranked by fewer than this many eligible boards are excluded
# from the consensus output. Keeps a single contrarian source from
# seeding fringe slots on the consensus board. Revisit this floor as
# more sources come online (see docs/consensus_phase_2_design.md).
MIN_SOURCES = 2


@dataclass(frozen=True)
class _PlayerAggregate:
    """Intermediate per-player rollup before consensus_rank is assigned."""

    player_id: int
    ranks: list[int]
    avg: float
    median: float
    high: int  # best rank any source gave (numerically smallest)
    low: int  # worst rank any source gave
    std_dev: float

    @property
    def num_sources(self) -> int:
        return len(self.ranks)


async def recompute_consensus(
    db: AsyncSession,
    *,
    draft_year: int,
    trigger: ConsensusTrigger,
) -> ConsensusSnapshot:
    """Run a full recompute for one draft year and persist a new snapshot.

    Args:
        db: Async session; caller owns the surrounding transaction.
        draft_year: Year to aggregate APPROVED boards for.
        trigger: What caused this recompute, stored on the snapshot.

    Returns:
        The newly-inserted ``ConsensusSnapshot``. Its ``id`` is
        populated. Per-player and per-source rows are written before
        return; query them via ``snapshot.id``.

    Notes:
        Eligible boards = the most recent APPROVED board per source for
        the draft year. Earlier boards from the same source are not
        counted (so a source that publishes weekly doesn't get extra
        weight). ``MIN_SOURCES`` gates inclusion in the consensus
        output, not in the analytics math (sources still get analytics
        for any player they ranked, even if that player didn't clear
        the floor).
    """
    eligible_boards = await _select_eligible_boards(db, draft_year=draft_year)

    snapshot = ConsensusSnapshot(
        draft_year=draft_year,
        computed_at=datetime.utcnow(),
        num_boards=len(eligible_boards),
        board_ids=[b.id for b in eligible_boards if b.id is not None],
        trigger=trigger,
    )
    db.add(snapshot)
    await db.flush()
    assert snapshot.id is not None

    if not eligible_boards:
        # Empty year: snapshot stands as an audit marker (num_boards=0)
        # with no child rows. Callers can still query it.
        return snapshot

    # Gather all entries from eligible boards in a single query.
    board_ids = [b.id for b in eligible_boards if b.id is not None]
    entry_rows = (
        await db.execute(
            select(  # type: ignore[call-overload]
                BigBoardEntry.board_id, BigBoardEntry.player_id, BigBoardEntry.rank
            ).where(BigBoardEntry.board_id.in_(board_ids))  # type: ignore[attr-defined]
        )
    ).all()

    # ranks_by_player: player_id -> list of (board_id, rank)
    ranks_by_player: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for row in entry_rows:
        ranks_by_player[row.player_id].append((row.board_id, row.rank))

    aggregates = _build_aggregates(ranks_by_player)
    included = [a for a in aggregates if a.num_sources >= MIN_SOURCES]
    sorted_aggregates = sorted(included, key=_consensus_sort_key)

    prev_ranks = await _previous_snapshot_ranks(
        db, draft_year=draft_year, exclude_snapshot_id=snapshot.id
    )

    for consensus_rank, agg in enumerate(sorted_aggregates, start=1):
        prev = prev_ranks.get(agg.player_id)
        delta = (prev - consensus_rank) if prev is not None else None
        db.add(
            BigBoardConsensus(
                snapshot_id=snapshot.id,
                draft_year=draft_year,
                player_id=agg.player_id,
                consensus_rank=consensus_rank,
                avg_rank=agg.avg,
                median_rank=agg.median,
                high_rank=agg.high,
                low_rank=agg.low,
                std_dev=agg.std_dev,
                num_sources=agg.num_sources,
                prev_rank=prev,
                rank_delta=delta,
            )
        )
    await db.flush()

    await _write_source_analytics(
        db,
        snapshot_id=snapshot.id,
        eligible_boards=eligible_boards,
        ranks_by_player=ranks_by_player,
        consensus_by_player={
            a.player_id: i + 1 for i, a in enumerate(sorted_aggregates)
        },
    )
    await db.flush()

    return snapshot


async def get_latest_consensus(
    db: AsyncSession,
    *,
    draft_year: int,
    limit: Optional[int] = None,
) -> list[BigBoardConsensus]:
    """Return the most recent snapshot's consensus rows ordered by rank."""
    latest_snapshot_id = await db.scalar(
        select(ConsensusSnapshot.id)  # type: ignore[call-overload]
        .where(ConsensusSnapshot.draft_year == draft_year)  # type: ignore[arg-type]
        .order_by(ConsensusSnapshot.computed_at.desc())  # type: ignore[attr-defined]
        .limit(1)
    )
    if latest_snapshot_id is None:
        return []
    stmt = (
        select(BigBoardConsensus)  # type: ignore[call-overload]
        .where(BigBoardConsensus.snapshot_id == latest_snapshot_id)  # type: ignore[arg-type]
        .order_by(BigBoardConsensus.consensus_rank)  # type: ignore[arg-type]
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    rows = await db.execute(stmt)
    return list(rows.scalars().all())


async def get_player_rank_history(
    db: AsyncSession,
    *,
    player_id: int,
    draft_year: int,
) -> list[BigBoardConsensus]:
    """Return all snapshot rows for one player, oldest-first.

    Feeds the Phase 3 trajectory chart on the player detail page.
    """
    rows = await db.execute(
        select(BigBoardConsensus)  # type: ignore[call-overload]
        .join(
            ConsensusSnapshot,
            ConsensusSnapshot.id == BigBoardConsensus.snapshot_id,  # type: ignore[arg-type]
        )
        .where(BigBoardConsensus.player_id == player_id)  # type: ignore[arg-type]
        .where(BigBoardConsensus.draft_year == draft_year)  # type: ignore[arg-type]
        .order_by(ConsensusSnapshot.computed_at)  # type: ignore[arg-type]
    )
    return list(rows.scalars().all())


async def _select_eligible_boards(
    db: AsyncSession, *, draft_year: int
) -> list[BigBoard]:
    """Return the most recent APPROVED board per source for the year."""
    # Postgres DISTINCT ON (news_source_id) ORDER BY published_at DESC
    # selects exactly one row per source — the most recent.
    stmt = (
        select(BigBoard)  # type: ignore[call-overload]
        .where(BigBoard.status == BoardStatus.APPROVED)  # type: ignore[arg-type]
        .where(BigBoard.draft_year == draft_year)  # type: ignore[arg-type]
        .distinct(BigBoard.news_source_id)  # type: ignore[arg-type]
        .order_by(BigBoard.news_source_id, BigBoard.published_at.desc())  # type: ignore[arg-type,attr-defined]
    )
    rows = await db.execute(stmt)
    return list(rows.scalars().all())


def _build_aggregates(
    ranks_by_player: dict[int, list[tuple[int, int]]],
) -> list[_PlayerAggregate]:
    out: list[_PlayerAggregate] = []
    for player_id, board_rank_pairs in ranks_by_player.items():
        ranks = [r for _, r in board_rank_pairs]
        std = statistics.stdev(ranks) if len(ranks) > 1 else 0.0
        out.append(
            _PlayerAggregate(
                player_id=player_id,
                ranks=ranks,
                avg=statistics.mean(ranks),
                median=statistics.median(ranks),
                high=min(ranks),
                low=max(ranks),
                std_dev=std,
            )
        )
    return out


def _consensus_sort_key(agg: _PlayerAggregate) -> tuple[float, float, int, int]:
    """Tie-breaker order: avg → median → high (best individual) → player_id.

    player_id is the stable final key so two snapshots over identical
    aggregate inputs produce identical orderings.
    """
    return (agg.avg, agg.median, agg.high, agg.player_id)


async def _previous_snapshot_ranks(
    db: AsyncSession,
    *,
    draft_year: int,
    exclude_snapshot_id: int,
) -> dict[int, int]:
    """Return ``player_id -> consensus_rank`` from the prior snapshot.

    Used to populate ``prev_rank`` and ``rank_delta`` so the
    trajectory chart and rising/falling badges fall out of a simple
    SELECT in Phase 3.
    """
    prior_id = await db.scalar(
        select(ConsensusSnapshot.id)  # type: ignore[call-overload]
        .where(ConsensusSnapshot.draft_year == draft_year)  # type: ignore[arg-type]
        .where(ConsensusSnapshot.id != exclude_snapshot_id)  # type: ignore[arg-type]
        .order_by(ConsensusSnapshot.computed_at.desc())  # type: ignore[attr-defined]
        .limit(1)
    )
    if prior_id is None:
        return {}
    rows = await db.execute(
        select(BigBoardConsensus.player_id, BigBoardConsensus.consensus_rank).where(  # type: ignore[call-overload]
            BigBoardConsensus.snapshot_id == prior_id
        )  # type: ignore[arg-type]
    )
    return {r.player_id: r.consensus_rank for r in rows.all()}


async def _write_source_analytics(
    db: AsyncSession,
    *,
    snapshot_id: int,
    eligible_boards: list[BigBoard],
    ranks_by_player: dict[int, list[tuple[int, int]]],
    consensus_by_player: dict[int, int],
) -> None:
    """Write one SourceAnalytics row per eligible board in the snapshot.

    avg_deviation only counts players that cleared MIN_SOURCES (i.e.,
    players that are on the consensus output). Sources that ranked a
    bunch of fringe one-source players don't get punished for them.
    """
    board_lookup = {b.id: b for b in eligible_boards if b.id is not None}

    # Per-source: list of (player_id, source_rank, consensus_rank).
    per_source: dict[int, list[tuple[int, int, int]]] = defaultdict(list)
    for player_id, board_rank_pairs in ranks_by_player.items():
        consensus_rank = consensus_by_player.get(player_id)
        if consensus_rank is None:
            continue  # player didn't clear MIN_SOURCES
        for board_id, source_rank in board_rank_pairs:
            board = board_lookup.get(board_id)
            if board is None or board.news_source_id is None:
                continue
            per_source[board.news_source_id].append(
                (player_id, source_rank, consensus_rank)
            )

    # First pass: compute avg_deviation + biggest_outlier per source.
    raw_by_source: dict[int, tuple[float, int, Optional[int], int]] = {}
    for source_id, triples in per_source.items():
        deviations = [abs(src - cons) for _, src, cons in triples]
        avg_dev = statistics.mean(deviations) if deviations else 0.0
        # Biggest outlier: the player with the largest absolute distance.
        # outlier_delta is consensus_rank - source_rank so positive means
        # the source ranked the player higher (lower number) than consensus.
        outlier_pid: Optional[int] = None
        outlier_delta = 0
        if triples:
            outlier_player_id, outlier_src, outlier_cons = max(
                triples, key=lambda t: abs(t[1] - t[2])
            )
            outlier_pid = outlier_player_id
            outlier_delta = outlier_cons - outlier_src

        latest_board_id = next(
            (
                b.id
                for b in eligible_boards
                if b.news_source_id == source_id and b.id is not None
            ),
            None,
        )
        if latest_board_id is None:
            continue
        raw_by_source[source_id] = (
            avg_dev,
            latest_board_id,
            outlier_pid,
            outlier_delta,
        )

    if not raw_by_source:
        return

    # Second pass: normalize avg_deviation into a contrarian z-score
    # across the sources in this snapshot. Positive = more contrarian
    # than the snapshot's average; zero when only one source is in play.
    avgs = [tup[0] for tup in raw_by_source.values()]
    mean_avg = statistics.mean(avgs)
    stdev_avg = statistics.stdev(avgs) if len(avgs) > 1 else 0.0

    for source_id, (
        avg_dev,
        latest_board_id,
        outlier_pid,
        outlier_delta,
    ) in raw_by_source.items():
        score = (avg_dev - mean_avg) / stdev_avg if stdev_avg > 0 else 0.0
        db.add(
            SourceAnalytics(
                snapshot_id=snapshot_id,
                news_source_id=source_id,
                latest_board_id=latest_board_id,
                avg_deviation=avg_dev,
                contrarian_score=score,
                biggest_outlier_player_id=outlier_pid,
                outlier_delta=outlier_delta,
            )
        )


async def delete_snapshot(db: AsyncSession, *, snapshot_id: int) -> None:
    """Remove a snapshot and its child rows.

    Useful for ops cleanup (e.g., pruning old snapshots). The FK
    cascade handles the child rows, but the explicit DELETE here keeps
    the audit trail consistent with what the service intends.
    """
    await db.execute(
        delete(ConsensusSnapshot).where(ConsensusSnapshot.id == snapshot_id)  # type: ignore[arg-type]
    )
