"""Integration tests for the consensus-page service helpers (ticket #270).

Exercises ``get_source_breakdown_matrix`` and ``get_rank_trajectories``
against a live Postgres test schema seeded with realistic consensus data.

Guard: integration tests require ``TEST_DATABASE_URL`` and
``PYTEST_ALLOW_DB=1`` — see ``tests/integration/conftest.py``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
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
from app.services import consensus_read_service as read_svc
from tests.integration.conftest import make_player


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Shared factories
# ---------------------------------------------------------------------------


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
    published_days_ago: float = 1,
) -> Board:
    """Insert an APPROVED board with the given entries directly."""
    assert source.id is not None
    board = Board(
        news_source_id=source.id,
        draft_year=draft_year,
        published_at=_now() - timedelta(days=published_days_ago),
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
    bbc_rows: list[tuple[PlayerMaster, int]],  # (player, consensus_rank)
    source_analytics: list[tuple[NewsSource, int, float, float]],  # (src, board_id, avg_dev, contrarian)
    computed_hours_ago: float = 0,
) -> ConsensusSnapshot:
    """Insert a ConsensusSnapshot and its BigBoardConsensus rows manually.

    This is a simplified factory that bypasses ``recompute_consensus`` so tests
    stay fast and deterministic — we only need the read-path to be correct.
    """
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

    for src, board_id, avg_dev, contrarian in source_analytics:
        assert src.id is not None
        db.add(
            SourceAnalytics(
                snapshot_id=snap.id,
                news_source_id=src.id,
                latest_board_id=board_id,
                avg_deviation=avg_dev,
                contrarian_score=contrarian,
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
async def three_players(db_session: AsyncSession) -> list[PlayerMaster]:
    """Insert three players and return them in rank order."""
    players = [
        make_player("Cooper", "Flagg", school="Duke"),
        make_player("Dylan", "Harper", school="Rutgers"),
        make_player("Ace", "Bailey", school="Kansas"),
    ]
    for p in players:
        db_session.add(p)
    await db_session.flush()
    return players


@pytest_asyncio.fixture()
async def two_sources(db_session: AsyncSession) -> list[NewsSource]:
    return [
        await _make_source(db_session, "the-athletic"),
        await _make_source(db_session, "espn"),
    ]


# ---------------------------------------------------------------------------
# get_source_breakdown_matrix — integration
# ---------------------------------------------------------------------------


class TestGetSourceBreakdownMatrixIntegration:
    """get_source_breakdown_matrix against a seeded multi-source snapshot."""

    @pytest.mark.asyncio
    async def test_cells_match_board_entries(
        self,
        db_session: AsyncSession,
        three_players: list[PlayerMaster],
        two_sources: list[NewsSource],
    ) -> None:
        """Matrix cells match the board entries that fed the snapshot.

        Source A ranks p1=1, p2=2, p3=3.
        Source B ranks p1=2, p2=1, p3=4.
        Consensus ranks: p1=1, p2=2, p3=3.
        Cells should reflect each source's rank for each player.
        """
        p1, p2, p3 = three_players
        src_a, src_b = two_sources

        board_a = await _make_board_with_entries(
            db_session,
            source=src_a,
            draft_year=2026,
            entries=[(p1, 1), (p2, 2), (p3, 3)],
        )
        board_b = await _make_board_with_entries(
            db_session,
            source=src_b,
            draft_year=2026,
            entries=[(p1, 2), (p2, 1), (p3, 4)],
        )
        assert board_a.id is not None and board_b.id is not None

        snap = await _make_snapshot(
            db_session,
            draft_year=2026,
            board_ids=[board_a.id, board_b.id],
            bbc_rows=[(p1, 1), (p2, 2), (p3, 3)],
            source_analytics=[
                (src_a, board_a.id, 0.0, 0.0),
                (src_b, board_b.id, 1.0, 1.0),
            ],
        )
        await db_session.commit()

        result = await read_svc.get_source_breakdown_matrix(
            db_session, draft_year=2026, top_n=5
        )

        assert len(result["players"]) == 3
        assert len(result["sources"]) == 2

        # Extract player and source ids
        player_ids = {r["player_id"] for r in result["players"]}
        source_ids = {s["source_id"] for s in result["sources"]}
        assert p1.id in player_ids
        assert p2.id in player_ids
        assert src_a.id in source_ids
        assert src_b.id in source_ids

        # Verify specific cell values
        assert p1.id is not None and src_a.id is not None and src_b.id is not None
        assert p3.id is not None
        cell_p1_a = result["cells"][(p1.id, src_a.id)]
        assert cell_p1_a["rank"] == 1  # source A had p1 at #1

        cell_p1_b = result["cells"][(p1.id, src_b.id)]
        assert cell_p1_b["rank"] == 2  # source B had p1 at #2

        cell_p3_b = result["cells"][(p3.id, src_b.id)]
        assert cell_p3_b["rank"] == 4  # source B had p3 at #4

    @pytest.mark.asyncio
    async def test_outlier_cell_flagged(
        self,
        db_session: AsyncSession,
        three_players: list[PlayerMaster],
        two_sources: list[NewsSource],
    ) -> None:
        """A cell more than 5 spots from consensus is flagged as an outlier.

        p3 has consensus_rank=3.
        Source B ranks p3 at position 10 → delta=7 → outlier='low'.
        """
        p1, p2, p3 = three_players
        src_a, src_b = two_sources

        board_a = await _make_board_with_entries(
            db_session,
            source=src_a,
            draft_year=2026,
            entries=[(p1, 1), (p2, 2), (p3, 3)],
        )
        board_b = await _make_board_with_entries(
            db_session,
            source=src_b,
            draft_year=2026,
            entries=[(p1, 1), (p2, 2), (p3, 10)],
        )
        assert board_a.id is not None and board_b.id is not None

        snap = await _make_snapshot(
            db_session,
            draft_year=2026,
            board_ids=[board_a.id, board_b.id],
            bbc_rows=[(p1, 1), (p2, 2), (p3, 3)],
            source_analytics=[
                (src_a, board_a.id, 0.0, 0.0),
                (src_b, board_b.id, 3.5, 1.2),
            ],
        )
        await db_session.commit()

        result = await read_svc.get_source_breakdown_matrix(
            db_session, draft_year=2026, top_n=5
        )

        assert p3.id is not None and src_b.id is not None
        cell = result["cells"][(p3.id, src_b.id)]
        assert cell["rank"] == 10
        assert cell["outlier"] == "low"

    @pytest.mark.asyncio
    async def test_empty_when_no_snapshot(
        self,
        db_session: AsyncSession,
    ) -> None:
        """Empty sentinel returned when no snapshot exists."""
        result = await read_svc.get_source_breakdown_matrix(
            db_session, draft_year=2099, top_n=10
        )
        assert result == {"players": [], "sources": [], "cells": {}}

    @pytest.mark.asyncio
    async def test_top_n_limits_player_rows(
        self,
        db_session: AsyncSession,
        three_players: list[PlayerMaster],
        two_sources: list[NewsSource],
    ) -> None:
        """top_n=2 returns only 2 player rows even with 3 ranked players."""
        p1, p2, p3 = three_players
        src_a, _ = two_sources

        board_a = await _make_board_with_entries(
            db_session,
            source=src_a,
            draft_year=2026,
            entries=[(p1, 1), (p2, 2), (p3, 3)],
        )
        assert board_a.id is not None

        snap = await _make_snapshot(
            db_session,
            draft_year=2026,
            board_ids=[board_a.id],
            bbc_rows=[(p1, 1), (p2, 2), (p3, 3)],
            source_analytics=[(src_a, board_a.id, 0.0, 0.0)],
        )
        await db_session.commit()

        result = await read_svc.get_source_breakdown_matrix(
            db_session, draft_year=2026, top_n=2
        )
        assert len(result["players"]) == 2
        player_ids = {r["player_id"] for r in result["players"]}
        # Top 2 should be p1 and p2 (consensus ranks 1 and 2)
        assert p1.id in player_ids
        assert p2.id in player_ids
        assert p3.id not in player_ids


# ---------------------------------------------------------------------------
# get_rank_trajectories — integration
# ---------------------------------------------------------------------------


class TestGetRankTrajectoriesIntegration:
    """get_rank_trajectories across ≥2 seeded snapshots."""

    @pytest.mark.asyncio
    async def test_series_reflects_rank_changes_across_two_snapshots(
        self,
        db_session: AsyncSession,
        three_players: list[PlayerMaster],
        two_sources: list[NewsSource],
    ) -> None:
        """Two snapshots → each player has a length-2 series with correct ranks.

        Snapshot 1 (older): p1=#1, p2=#2, p3=#3.
        Snapshot 2 (newer): p1=#2, p2=#1, p3=#3 (p1 and p2 swap).
        """
        p1, p2, p3 = three_players
        src_a, src_b = two_sources

        board_a = await _make_board_with_entries(
            db_session,
            source=src_a,
            draft_year=2026,
            entries=[(p1, 1), (p2, 2), (p3, 3)],
        )
        board_b = await _make_board_with_entries(
            db_session,
            source=src_b,
            draft_year=2026,
            entries=[(p1, 2), (p2, 1), (p3, 3)],
        )
        assert board_a.id is not None and board_b.id is not None

        snap1 = await _make_snapshot(
            db_session,
            draft_year=2026,
            board_ids=[board_a.id],
            bbc_rows=[(p1, 1), (p2, 2), (p3, 3)],
            source_analytics=[(src_a, board_a.id, 0.0, 0.0)],
            computed_hours_ago=48,
        )
        snap2 = await _make_snapshot(
            db_session,
            draft_year=2026,
            board_ids=[board_a.id, board_b.id],
            bbc_rows=[(p1, 2), (p2, 1), (p3, 3)],
            source_analytics=[
                (src_a, board_a.id, 0.5, 0.5),
                (src_b, board_b.id, 0.5, -0.5),
            ],
            computed_hours_ago=0,
        )
        await db_session.commit()

        result = await read_svc.get_rank_trajectories(
            db_session, draft_year=2026, top_n=5
        )

        assert len(result) == 3

        # Find p1 entry
        assert p1.id is not None
        p1_entry = next(r for r in result if r["player_id"] == p1.id)
        assert len(p1_entry["series"]) == 2
        # Oldest first
        assert p1_entry["series"][0]["consensus_rank"] == 1  # snap1
        assert p1_entry["series"][1]["consensus_rank"] == 2  # snap2

        # Find p2 entry
        assert p2.id is not None
        p2_entry = next(r for r in result if r["player_id"] == p2.id)
        assert len(p2_entry["series"]) == 2
        assert p2_entry["series"][0]["consensus_rank"] == 2  # snap1
        assert p2_entry["series"][1]["consensus_rank"] == 1  # snap2

    @pytest.mark.asyncio
    async def test_single_snapshot_returns_length_one_series(
        self,
        db_session: AsyncSession,
        three_players: list[PlayerMaster],
        two_sources: list[NewsSource],
    ) -> None:
        """Single snapshot → all players have a length-1 series (flat trajectory)."""
        p1, p2, p3 = three_players
        src_a, _ = two_sources

        board_a = await _make_board_with_entries(
            db_session,
            source=src_a,
            draft_year=2026,
            entries=[(p1, 1), (p2, 2), (p3, 3)],
        )
        assert board_a.id is not None

        snap = await _make_snapshot(
            db_session,
            draft_year=2026,
            board_ids=[board_a.id],
            bbc_rows=[(p1, 1), (p2, 2), (p3, 3)],
            source_analytics=[(src_a, board_a.id, 0.0, 0.0)],
        )
        await db_session.commit()

        result = await read_svc.get_rank_trajectories(
            db_session, draft_year=2026, top_n=5
        )

        assert len(result) == 3
        for entry in result:
            assert len(entry["series"]) == 1

    @pytest.mark.asyncio
    async def test_ordered_by_current_consensus_rank(
        self,
        db_session: AsyncSession,
        three_players: list[PlayerMaster],
        two_sources: list[NewsSource],
    ) -> None:
        """Results are ordered by consensus_rank ascending (latest snapshot)."""
        p1, p2, p3 = three_players
        src_a, _ = two_sources

        board_a = await _make_board_with_entries(
            db_session,
            source=src_a,
            draft_year=2026,
            entries=[(p1, 1), (p2, 2), (p3, 3)],
        )
        assert board_a.id is not None

        snap = await _make_snapshot(
            db_session,
            draft_year=2026,
            board_ids=[board_a.id],
            bbc_rows=[(p1, 1), (p2, 2), (p3, 3)],
            source_analytics=[(src_a, board_a.id, 0.0, 0.0)],
        )
        await db_session.commit()

        result = await read_svc.get_rank_trajectories(
            db_session, draft_year=2026, top_n=5
        )

        assert p1.id is not None and p2.id is not None and p3.id is not None
        assert [r["player_id"] for r in result] == [p1.id, p2.id, p3.id]

    @pytest.mark.asyncio
    async def test_series_computed_at_oldest_first(
        self,
        db_session: AsyncSession,
        three_players: list[PlayerMaster],
        two_sources: list[NewsSource],
    ) -> None:
        """computed_at timestamps increase monotonically (oldest-first)."""
        p1, p2, p3 = three_players
        src_a, src_b = two_sources

        board_a = await _make_board_with_entries(
            db_session,
            source=src_a,
            draft_year=2026,
            entries=[(p1, 1), (p2, 2), (p3, 3)],
        )
        board_b = await _make_board_with_entries(
            db_session,
            source=src_b,
            draft_year=2026,
            entries=[(p1, 1), (p2, 2), (p3, 3)],
        )
        assert board_a.id is not None and board_b.id is not None

        snap1 = await _make_snapshot(
            db_session,
            draft_year=2026,
            board_ids=[board_a.id],
            bbc_rows=[(p1, 1), (p2, 2), (p3, 3)],
            source_analytics=[(src_a, board_a.id, 0.0, 0.0)],
            computed_hours_ago=72,
        )
        snap2 = await _make_snapshot(
            db_session,
            draft_year=2026,
            board_ids=[board_a.id, board_b.id],
            bbc_rows=[(p1, 1), (p2, 2), (p3, 3)],
            source_analytics=[
                (src_a, board_a.id, 0.0, 0.0),
                (src_b, board_b.id, 0.0, 0.0),
            ],
            computed_hours_ago=0,
        )
        await db_session.commit()

        assert p1.id is not None
        result = await read_svc.get_rank_trajectories(
            db_session, draft_year=2026, top_n=5
        )
        p1_series = next(r for r in result if r["player_id"] == p1.id)["series"]
        assert len(p1_series) == 2
        assert p1_series[0]["computed_at"] < p1_series[1]["computed_at"]

    @pytest.mark.asyncio
    async def test_empty_when_no_snapshot(
        self,
        db_session: AsyncSession,
    ) -> None:
        """Returns empty list when no snapshot exists for the draft year."""
        result = await read_svc.get_rank_trajectories(
            db_session, draft_year=2099, top_n=10
        )
        assert result == []
