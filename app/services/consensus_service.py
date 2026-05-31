"""Phase 2 consensus computation service.

Aggregates the most recent APPROVED big board per source into a single
ranked consensus board for a draft year, plus per-source deviation
analytics. Append-only snapshots so rank trajectory falls out of a
simple query in Phase 3.

See ``docs/consensus_phase_2_design.md`` for the algorithm rationale.
Tier values stored on board_entries are intentionally ignored in
the aggregation — tier is admin-side transcription fidelity, not a
consensus signal in v1.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.boards import Board, BoardEntry, BoardStatus
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
    as_of: Optional[datetime] = None,
) -> ConsensusSnapshot:
    """Run a full recompute for one draft year and persist a new snapshot.

    Args:
        db: Async session; caller owns the surrounding transaction.
        draft_year: Year to aggregate APPROVED boards for.
        trigger: What caused this recompute, stored on the snapshot.
        as_of: Optional point-in-time ceiling. When set, only boards
            published at or before this instant are eligible, and the
            snapshot's ``computed_at`` is stamped with it. This lets a
            backfill driver reconstruct what the consensus looked like
            on a past date by replaying snapshots oldest-first. When
            ``None`` (the live path) the recompute uses all current
            boards and stamps ``computed_at`` with now.

    Returns:
        The newly-inserted ``ConsensusSnapshot``. Its ``id`` is
        populated. Per-player and per-source rows are written before
        return; query them via ``snapshot.id``.

    Notes:
        Eligible boards = the most recent APPROVED board per source for
        the draft year (and, with ``as_of``, published at or before that
        instant). Earlier boards from the same source are not counted
        (so a source that publishes weekly doesn't get extra weight).
        ``MIN_SOURCES`` gates inclusion in the consensus output, not in
        the analytics math (sources still get analytics for any player
        they ranked, even if that player didn't clear the floor).
    """
    computed_at = as_of if as_of is not None else datetime.utcnow()
    eligible_boards = await _select_eligible_boards(
        db, draft_year=draft_year, as_of=as_of
    )

    snapshot = ConsensusSnapshot(
        draft_year=draft_year,
        computed_at=computed_at,
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

    # Gather resolved entries from eligible boards in a single query.
    # UNRESOLVED entries (player_id IS NULL) carry no identity and must not
    # feed the aggregation — otherwise they collapse under a single NULL key
    # and, if that pseudo-player clears MIN_SOURCES, produce a consensus row
    # with a NULL player_id (NOT NULL violation). They simply don't count
    # until an admin resolves them.
    board_ids = [b.id for b in eligible_boards if b.id is not None]
    entry_rows = (
        await db.execute(
            select(  # type: ignore[call-overload]
                BoardEntry.board_id, BoardEntry.player_id, BoardEntry.position
            )
            .where(BoardEntry.board_id.in_(board_ids))  # type: ignore[attr-defined]
            .where(BoardEntry.player_id.is_not(None))  # type: ignore[union-attr]
        )
    ).all()

    # ranks_by_player: player_id -> list of (board_id, rank)
    ranks_by_player: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for row in entry_rows:
        ranks_by_player[row.player_id].append((row.board_id, row.position))

    aggregates = _build_aggregates(ranks_by_player)
    included = [a for a in aggregates if a.num_sources >= MIN_SOURCES]
    sorted_aggregates = sorted(included, key=_consensus_sort_key)

    prev_ranks = await _previous_snapshot_ranks(
        db,
        draft_year=draft_year,
        before=computed_at,
        exclude_snapshot_id=snapshot.id,
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
    db: AsyncSession, *, draft_year: int, as_of: Optional[datetime] = None
) -> list[Board]:
    """Return the most recent APPROVED board per source for the year.

    When ``as_of`` is set, only boards published at or before that
    instant are considered, so the result reconstructs the eligible set
    as it stood on that date (used by the historical backfill driver).

    Tie-breakers on identical ``published_at`` are deterministic so a
    re-run over unchanged data produces the same eligible-board set
    (and therefore the same consensus output and rank deltas):
        1. published_at DESC  -- the actual recency signal
        2. approved_at DESC   -- which board got approved later wins
        3. id DESC            -- final stable break, in case of identical
                                 approval timestamps from a batch import
    """
    stmt = (
        select(Board)  # type: ignore[call-overload]
        .where(Board.status == BoardStatus.APPROVED)  # type: ignore[arg-type]
        .where(Board.draft_year == draft_year)  # type: ignore[arg-type]
        .distinct(Board.news_source_id)  # type: ignore[arg-type]
    )
    if as_of is not None:
        stmt = stmt.where(Board.published_at <= as_of)  # type: ignore[arg-type]
    stmt = stmt.order_by(
        Board.news_source_id,  # type: ignore[arg-type]
        Board.published_at.desc(),  # type: ignore[attr-defined]
        Board.approved_at.desc(),  # type: ignore[union-attr]
        Board.id.desc(),  # type: ignore[union-attr]
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


def _rankdata(values: list[float]) -> list[float]:
    """Return 1-based ranks of ``values``, averaging ties.

    Mirrors ``scipy.stats.rankdata`` (average method) without the dependency.
    """
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(values):
        j = i
        while j + 1 < len(values) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0  # 1-based average rank for the tie group
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        i = j + 1
    return ranks


def _pearson(xs: list[float], ys: list[float]) -> Optional[float]:
    """Pearson correlation of two equal-length sequences, or None if undefined."""
    n = len(xs)
    if n < 2:
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x <= 0 or var_y <= 0:
        return None  # one variable is constant → correlation undefined
    return cov / ((var_x**0.5) * (var_y**0.5))


def _spearman(pairs: Sequence[tuple[float, float]]) -> Optional[float]:
    """Spearman rank correlation for (x, y) pairs.

    Computed as Pearson on the rank-transformed values — the correct measure
    for comparing two orderings (a source's board vs. the consensus). Returns
    None when fewer than 3 shared points exist (too small to be meaningful).
    """
    if len(pairs) < 3:
        return None
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    return _pearson(_rankdata(xs), _rankdata(ys))


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
    before: datetime,
    exclude_snapshot_id: int,
) -> dict[int, int]:
    """Return ``player_id -> consensus_rank`` from the prior snapshot.

    The "prior" snapshot is the most recent one for the year whose
    ``computed_at`` is at or before ``before`` (the current snapshot's
    own timestamp), excluding the current snapshot itself. Anchoring on
    ``before`` rather than "the most recent other snapshot" keeps deltas
    correct when snapshots are written out of insertion order — e.g. the
    backfill driver replaying history oldest-first, where a later-dated
    snapshot may already exist. On the live append path (``before`` =
    now) this resolves to the immediately-preceding snapshot, unchanged.

    Used to populate ``prev_rank`` and ``rank_delta`` so the trajectory
    chart and rising/falling badges fall out of a simple SELECT.
    """
    prior_id = await db.scalar(
        select(ConsensusSnapshot.id)  # type: ignore[call-overload]
        .where(ConsensusSnapshot.draft_year == draft_year)  # type: ignore[arg-type]
        .where(ConsensusSnapshot.id != exclude_snapshot_id)  # type: ignore[arg-type]
        .where(ConsensusSnapshot.computed_at <= before)  # type: ignore[arg-type]
        .order_by(
            ConsensusSnapshot.computed_at.desc(),  # type: ignore[attr-defined]
            ConsensusSnapshot.id.desc(),  # type: ignore[union-attr]
        )
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
    eligible_boards: list[Board],
    ranks_by_player: dict[int, list[tuple[int, int]]],
    consensus_by_player: dict[int, int],
) -> None:
    """Write one SourceAnalytics row per eligible board in the snapshot.

    Every eligible source gets a row, even when MIN_SOURCES gates all
    of its players out of the consensus (e.g., early in the season
    when only one board is approved). In that case ``avg_deviation``
    is 0.0 and ``biggest_outlier_player_id`` is NULL — the row stands
    as a "we saw this source but had nothing to compare against"
    marker so the per-source-per-snapshot invariant holds.

    ``avg_deviation`` only counts players that cleared MIN_SOURCES so
    sources aren't penalized for ranking fringe one-board players.
    """
    board_lookup = {b.id: b for b in eligible_boards if b.id is not None}

    # Pre-build the per-source skeleton from eligible_boards so every
    # eligible source ends up with a row, even if it has no players
    # in the consensus output.
    source_to_board: dict[int, int] = {}
    for board in eligible_boards:
        if board.id is None or board.news_source_id is None:
            continue
        # eligible_boards is one row per source already (DISTINCT ON),
        # so the first hit is the only hit.
        source_to_board.setdefault(board.news_source_id, board.id)

    if not source_to_board:
        return

    # Per-source: list of (player_id, source_rank, consensus_rank).
    # Only triples for players that cleared MIN_SOURCES contribute to
    # avg_deviation / biggest_outlier; absent sources still get a row
    # downstream with avg_deviation=0.
    per_source: dict[int, list[tuple[int, int, int]]] = defaultdict(list)
    for player_id, board_rank_pairs in ranks_by_player.items():
        consensus_rank = consensus_by_player.get(player_id)
        if consensus_rank is None:
            continue  # player didn't clear MIN_SOURCES
        for board_id, source_rank in board_rank_pairs:
            entry_board = board_lookup.get(board_id)
            if entry_board is None or entry_board.news_source_id is None:
                continue
            per_source[entry_board.news_source_id].append(
                (player_id, source_rank, consensus_rank)
            )

    # First pass: compute avg_deviation + biggest_outlier per source.
    # Iterate over source_to_board so every eligible source is included,
    # not just those that contributed to consensus.
    raw_by_source: dict[
        int, tuple[float, int, Optional[int], int, Optional[float]]
    ] = {}
    for source_id, latest_board_id in source_to_board.items():
        triples = per_source.get(source_id, [])
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

        # Alignment: Spearman correlation of (source_rank, consensus_rank) over
        # the players this source shares with the consensus. Captures how well
        # the source's *ordering* tracks the field, independent of avg_deviation.
        alignment = _spearman([(src, cons) for _, src, cons in triples])

        raw_by_source[source_id] = (
            avg_dev,
            latest_board_id,
            outlier_pid,
            outlier_delta,
            alignment,
        )

    # Second pass: normalize avg_deviation into a contrarian z-score
    # across the sources in this snapshot. Positive = more contrarian
    # than the snapshot's average; zero when only one source is in play
    # or every source has the same (often zero) deviation.
    avgs = [tup[0] for tup in raw_by_source.values()]
    mean_avg = statistics.mean(avgs) if avgs else 0.0
    stdev_avg = statistics.stdev(avgs) if len(avgs) > 1 else 0.0

    for source_id, (
        avg_dev,
        latest_board_id,
        outlier_pid,
        outlier_delta,
        alignment,
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
                alignment=alignment,
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
