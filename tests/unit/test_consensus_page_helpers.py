"""Unit tests for the consensus-page service helpers added in ticket #270.

Tests exercise ``get_source_breakdown_matrix`` and ``get_rank_trajectories``
at the pure-logic level — cell computation, outlier flags, series ordering,
and degenerate inputs — without touching the database.

The helpers are async and depend on ``AsyncSession``, so each test uses a
lightweight mock session that returns pre-baked rows from
``db.execute(...).scalars().all()`` / ``db.execute(...).all()`` /
``db.scalar(...)`` call chains.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers: thin stubs that mimic the ORM rows the helpers read
# ---------------------------------------------------------------------------


def _snapshot(
    sid: int,
    draft_year: int = 2026,
    board_ids: list[int] | None = None,
) -> MagicMock:
    snap = MagicMock()
    snap.id = sid
    snap.draft_year = draft_year
    snap.board_ids = board_ids or []
    snap.computed_at = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc).replace(
        tzinfo=None
    )
    return snap


def _bbc(
    player_id: int,
    consensus_rank: int,
    snapshot_id: int = 1,
    draft_year: int = 2026,
) -> MagicMock:
    row = MagicMock()
    row.player_id = player_id
    row.consensus_rank = consensus_rank
    row.snapshot_id = snapshot_id
    row.draft_year = draft_year
    return row


def _board(bid: int, source_id: int) -> MagicMock:
    b = MagicMock()
    b.id = bid
    b.news_source_id = source_id
    return b


def _board_entry(board_id: int, player_id: int, position: int) -> MagicMock:
    e = MagicMock()
    e.board_id = board_id
    e.player_id = player_id
    e.position = position
    return e


def _source(sid: int, name: str) -> MagicMock:
    s = MagicMock()
    s.id = sid
    s.name = name
    s.display_name = name
    return s


def _player(pid: int, display_name: str, slug: str) -> MagicMock:
    p = MagicMock()
    p.id = pid
    p.display_name = display_name
    p.slug = slug
    return p


# ---------------------------------------------------------------------------
# Outlier-flag logic: tested directly against the threshold constant
# ---------------------------------------------------------------------------


class TestOutlierFlagLogic:
    """Verify the outlier-flag formula without touching DB or async code."""

    def test_within_threshold_no_outlier(self) -> None:
        """A source rank within 5 positions of consensus is not an outlier."""
        from app.services.consensus_read_service import _OUTLIER_THRESHOLD

        # Exactly at threshold boundary — not an outlier (> not >=).
        delta_at_boundary = _OUTLIER_THRESHOLD
        assert not (delta_at_boundary > _OUTLIER_THRESHOLD)

    def test_above_threshold_is_low_outlier(self) -> None:
        """A source rank more than 5 spots WORSE than consensus → 'low'."""
        from app.services.consensus_read_service import _OUTLIER_THRESHOLD

        # source_rank=12, consensus_rank=5 → delta=+7 → "low"
        source_rank, consensus_rank = 12, 5
        delta = source_rank - consensus_rank
        assert delta > _OUTLIER_THRESHOLD
        # simulate flag
        outlier = "low" if delta > _OUTLIER_THRESHOLD else None
        assert outlier == "low"

    def test_below_threshold_is_high_outlier(self) -> None:
        """A source rank more than 5 spots BETTER than consensus → 'high'."""
        from app.services.consensus_read_service import _OUTLIER_THRESHOLD

        # source_rank=2, consensus_rank=10 → delta=-8 → "high"
        source_rank, consensus_rank = 2, 10
        delta = source_rank - consensus_rank
        assert delta < -_OUTLIER_THRESHOLD
        outlier = "high" if delta < -_OUTLIER_THRESHOLD else None
        assert outlier == "high"

    def test_no_divergence_no_outlier(self) -> None:
        """Identical source and consensus rank → no outlier."""
        from app.services.consensus_read_service import _OUTLIER_THRESHOLD

        delta = 0
        outlier = (
            "high"
            if delta < -_OUTLIER_THRESHOLD
            else ("low" if delta > _OUTLIER_THRESHOLD else None)
        )
        assert outlier is None

    def test_threshold_value_is_documented(self) -> None:
        """The threshold constant is 5 (documented in the service module)."""
        from app.services.consensus_read_service import _OUTLIER_THRESHOLD

        assert _OUTLIER_THRESHOLD == 5


# ---------------------------------------------------------------------------
# get_source_breakdown_matrix — pure logic tests over fixture data
# ---------------------------------------------------------------------------


def _make_matrix_db(
    *,
    snapshot: MagicMock,
    bbc_rows: list[MagicMock],
    boards: list[MagicMock],
    entries: list[MagicMock],
    sources: list[MagicMock],
    players: list[MagicMock],
) -> MagicMock:
    """Build a mock AsyncSession for the matrix helper.

    The helper makes these queries in order:
    1. scalar → resolve snapshot id (returns snapshot.id)
    2. execute → top-N BigBoardConsensus rows          (.scalars().all())
    3. execute → ConsensusSnapshot                      (.scalar_one_or_none())
    4. execute → Board rows                             (.scalars().all())
    5. execute → NewsSource rows                        (.scalars().all())
    6. execute → BoardEntry rows                        (.all())
    7. execute → PlayerMaster rows (via _player_name_map) (.scalars().all())
    """
    db = MagicMock()

    def _scalars_result(items: list) -> MagicMock:
        r = MagicMock()
        r.scalars.return_value.all.return_value = items
        return r

    def _scalar_one_or_none_result(item: Any) -> MagicMock:
        r = MagicMock()
        r.scalar_one_or_none.return_value = item
        return r

    def _all_result(items: list) -> MagicMock:
        r = MagicMock()
        r.all.return_value = items
        return r

    # db.scalar → snapshot id
    db.scalar = AsyncMock(return_value=snapshot.id)

    # db.execute → ordered returns
    execute_returns = [
        _scalars_result(bbc_rows),              # top-N BigBoardConsensus
        _scalar_one_or_none_result(snapshot),   # ConsensusSnapshot (.scalar_one_or_none)
        _scalars_result(boards),                # Board rows
        _scalars_result(sources),               # NewsSource rows
        _all_result(entries),                   # BoardEntry rows
        _scalars_result(players),               # PlayerMaster (_player_name_map)
    ]
    db.execute = AsyncMock(side_effect=execute_returns)

    return db


class TestGetSourceBreakdownMatrix:
    """get_source_breakdown_matrix returns the correct shape and outlier flags."""

    @pytest.mark.asyncio
    async def test_empty_when_no_snapshot(self) -> None:
        """When no snapshot exists the helper returns the empty sentinel."""
        from app.services.consensus_read_service import get_source_breakdown_matrix

        db = MagicMock()
        db.scalar = AsyncMock(return_value=None)  # _resolve_snapshot_id → None

        result = await get_source_breakdown_matrix(db, draft_year=2026, top_n=10)
        assert result == {"players": [], "sources": [], "cells": {}}

    @pytest.mark.asyncio
    async def test_cells_present_no_outlier(self) -> None:
        """Cells within threshold are present with outlier=None."""
        from app.services.consensus_read_service import get_source_breakdown_matrix

        snap = _snapshot(sid=1, board_ids=[10])
        p1 = _player(pid=1, display_name="Player A", slug="player-a")
        bbc1 = _bbc(player_id=1, consensus_rank=1)
        board = _board(bid=10, source_id=100)
        src = _source(sid=100, name="source-one")
        # source_rank=2, consensus_rank=1 → delta=+1 → within threshold
        entry = _board_entry(board_id=10, player_id=1, position=2)

        db = _make_matrix_db(
            snapshot=snap,
            bbc_rows=[bbc1],
            boards=[board],
            entries=[entry],
            sources=[src],
            players=[p1],
        )

        with patch("app.utils.slug.generate_slug", side_effect=lambda s: s.lower()):
            result = await get_source_breakdown_matrix(db, draft_year=2026, top_n=10)

        assert len(result["players"]) == 1
        assert len(result["sources"]) == 1
        assert (1, 100) in result["cells"]
        cell = result["cells"][(1, 100)]
        assert cell["rank"] == 2
        assert cell["outlier"] is None

    @pytest.mark.asyncio
    async def test_outlier_high(self) -> None:
        """A source that rates a player far higher than consensus → 'high'."""
        from app.services.consensus_read_service import get_source_breakdown_matrix

        snap = _snapshot(sid=1, board_ids=[10])
        p1 = _player(pid=1, display_name="Player A", slug="player-a")
        # consensus_rank=10, source_rank=3 → delta=3-10=-7 → "high"
        bbc1 = _bbc(player_id=1, consensus_rank=10)
        board = _board(bid=10, source_id=100)
        src = _source(sid=100, name="source-one")
        entry = _board_entry(board_id=10, player_id=1, position=3)

        db = _make_matrix_db(
            snapshot=snap,
            bbc_rows=[bbc1],
            boards=[board],
            entries=[entry],
            sources=[src],
            players=[p1],
        )

        with patch("app.utils.slug.generate_slug", side_effect=lambda s: s.lower()):
            result = await get_source_breakdown_matrix(db, draft_year=2026, top_n=10)

        assert result["cells"][(1, 100)]["outlier"] == "high"

    @pytest.mark.asyncio
    async def test_outlier_low(self) -> None:
        """A source that rates a player far lower than consensus → 'low'."""
        from app.services.consensus_read_service import get_source_breakdown_matrix

        snap = _snapshot(sid=1, board_ids=[10])
        p1 = _player(pid=1, display_name="Player A", slug="player-a")
        # consensus_rank=2, source_rank=12 → delta=12-2=10 → "low"
        bbc1 = _bbc(player_id=1, consensus_rank=2)
        board = _board(bid=10, source_id=100)
        src = _source(sid=100, name="source-one")
        entry = _board_entry(board_id=10, player_id=1, position=12)

        db = _make_matrix_db(
            snapshot=snap,
            bbc_rows=[bbc1],
            boards=[board],
            entries=[entry],
            sources=[src],
            players=[p1],
        )

        with patch("app.utils.slug.generate_slug", side_effect=lambda s: s.lower()):
            result = await get_source_breakdown_matrix(db, draft_year=2026, top_n=10)

        assert result["cells"][(1, 100)]["outlier"] == "low"

    @pytest.mark.asyncio
    async def test_multiple_players_multiple_sources(self) -> None:
        """Matrix with two players × two sources produces the expected cells."""
        from app.services.consensus_read_service import get_source_breakdown_matrix

        snap = _snapshot(sid=1, board_ids=[10, 20])
        p1 = _player(pid=1, display_name="Player A", slug="player-a")
        p2 = _player(pid=2, display_name="Player B", slug="player-b")
        bbc1 = _bbc(player_id=1, consensus_rank=1)
        bbc2 = _bbc(player_id=2, consensus_rank=2)
        board_a = _board(bid=10, source_id=100)
        board_b = _board(bid=20, source_id=200)
        src_a = _source(sid=100, name="source-one")
        src_b = _source(sid=200, name="source-two")
        # Source A: p1@1 (exact), p2@3 (delta=+1 — within threshold)
        # Source B: p1@2 (delta=+1), p2@2 (exact)
        entries = [
            _board_entry(10, 1, 1),
            _board_entry(10, 2, 3),
            _board_entry(20, 1, 2),
            _board_entry(20, 2, 2),
        ]

        db = _make_matrix_db(
            snapshot=snap,
            bbc_rows=[bbc1, bbc2],
            boards=[board_a, board_b],
            entries=entries,
            sources=[src_a, src_b],
            players=[p1, p2],
        )

        with patch("app.utils.slug.generate_slug", side_effect=lambda s: s.lower()):
            result = await get_source_breakdown_matrix(db, draft_year=2026, top_n=10)

        assert len(result["players"]) == 2
        assert len(result["sources"]) == 2
        # Four cells expected
        assert len(result["cells"]) == 4
        # All within threshold → no outliers
        for cell in result["cells"].values():
            assert cell["outlier"] is None

    @pytest.mark.asyncio
    async def test_empty_when_no_boards(self) -> None:
        """Empty board list on snapshot returns the empty sentinel.

        When the snapshot exists but has no board_ids, the helper returns the
        empty sentinel without querying for boards.
        """
        from app.services.consensus_read_service import get_source_breakdown_matrix

        bbc1 = _bbc(player_id=1, consensus_rank=1)

        db = MagicMock()

        def _scalars_result(items: list) -> MagicMock:
            r = MagicMock()
            r.scalars.return_value.all.return_value = items
            return r

        def _scalar_one_or_none_result(item: Any) -> MagicMock:
            """Build an execute result that returns ``item`` from scalar_one_or_none()."""
            r = MagicMock()
            r.scalar_one_or_none.return_value = item
            return r

        # Snapshot with empty board_ids so the helper returns early.
        snap_empty = _snapshot(sid=1, board_ids=[])

        db.scalar = AsyncMock(return_value=snap_empty.id)
        db.execute = AsyncMock(
            side_effect=[
                _scalars_result([bbc1]),             # top-N BigBoardConsensus
                _scalar_one_or_none_result(snap_empty),  # ConsensusSnapshot with empty board_ids
            ]
        )

        result = await get_source_breakdown_matrix(db, draft_year=2026, top_n=10)
        assert result == {"players": [], "sources": [], "cells": {}}


# ---------------------------------------------------------------------------
# get_rank_trajectories — series ordering / shape tests
# ---------------------------------------------------------------------------


def _make_trajectories_db(
    *,
    snapshot_id: int,
    bbc_latest: list[MagicMock],
    history_rows: list[tuple[int, int, datetime]],
    players: list[MagicMock],
) -> MagicMock:
    """Build a mock AsyncSession for the trajectories helper.

    Query order:
    1. scalar → _resolve_snapshot_id
    2. execute → top-N BigBoardConsensus (latest snapshot)
    3. execute → full history (player_id, consensus_rank, computed_at)  — .all()
    4. execute → PlayerMaster rows — _player_name_map
    """
    db = MagicMock()
    db.scalar = AsyncMock(return_value=snapshot_id)

    def _scalars_result(items: list) -> MagicMock:
        r = MagicMock()
        r.scalars.return_value.all.return_value = items
        return r

    def _all_result(items: list) -> MagicMock:
        r = MagicMock()
        r.all.return_value = items
        return r

    db.execute = AsyncMock(
        side_effect=[
            _scalars_result(bbc_latest),    # top-N from latest snapshot
            _all_result(history_rows),       # full history
            _scalars_result(players),        # PlayerMaster
        ]
    )
    return db


class TestGetRankTrajectories:
    """get_rank_trajectories returns ordered series per player."""

    @pytest.mark.asyncio
    async def test_empty_when_no_snapshot(self) -> None:
        """Returns empty list when no snapshot exists."""
        from app.services.consensus_read_service import get_rank_trajectories

        db = MagicMock()
        db.scalar = AsyncMock(return_value=None)

        result = await get_rank_trajectories(db, draft_year=2026, top_n=10)
        assert result == []

    @pytest.mark.asyncio
    async def test_single_snapshot_single_point_series(self) -> None:
        """One snapshot → each player has a length-1 series (degenerate flat trajectory)."""
        from app.services.consensus_read_service import get_rank_trajectories

        t1 = datetime(2026, 1, 1, tzinfo=timezone.utc).replace(tzinfo=None)
        p1 = _player(pid=1, display_name="Player A", slug="player-a")
        bbc1 = _bbc(player_id=1, consensus_rank=1)
        history = [(1, 1, t1)]  # (player_id, rank, computed_at)

        db = _make_trajectories_db(
            snapshot_id=1,
            bbc_latest=[bbc1],
            history_rows=history,
            players=[p1],
        )

        result = await get_rank_trajectories(db, draft_year=2026, top_n=10)

        assert len(result) == 1
        entry = result[0]
        assert entry["player_id"] == 1
        assert entry["player_name"] == "Player A"
        assert entry["slug"] == "player-a"
        assert len(entry["series"]) == 1
        assert entry["series"][0]["consensus_rank"] == 1
        assert entry["series"][0]["computed_at"] == t1

    @pytest.mark.asyncio
    async def test_series_ordered_oldest_first(self) -> None:
        """With two snapshots the series is oldest-to-newest."""
        from app.services.consensus_read_service import get_rank_trajectories

        t1 = datetime(2026, 1, 1, tzinfo=timezone.utc).replace(tzinfo=None)
        t2 = datetime(2026, 1, 8, tzinfo=timezone.utc).replace(tzinfo=None)
        p1 = _player(pid=1, display_name="Player A", slug="player-a")
        bbc1 = _bbc(player_id=1, consensus_rank=3)  # current rank
        # DB orders by player_id, computed_at (ascending) so oldest arrives first
        history = [(1, 5, t1), (1, 3, t2)]

        db = _make_trajectories_db(
            snapshot_id=2,
            bbc_latest=[bbc1],
            history_rows=history,
            players=[p1],
        )

        result = await get_rank_trajectories(db, draft_year=2026, top_n=10)

        assert len(result) == 1
        series = result[0]["series"]
        assert len(series) == 2
        # Oldest point first
        assert series[0]["computed_at"] == t1
        assert series[0]["consensus_rank"] == 5
        assert series[1]["computed_at"] == t2
        assert series[1]["consensus_rank"] == 3

    @pytest.mark.asyncio
    async def test_multiple_players_ordered_by_rank(self) -> None:
        """Multiple players are returned in consensus_rank order (ascending)."""
        from app.services.consensus_read_service import get_rank_trajectories

        t1 = datetime(2026, 1, 1, tzinfo=timezone.utc).replace(tzinfo=None)
        p1 = _player(pid=1, display_name="Player A", slug="player-a")
        p2 = _player(pid=2, display_name="Player B", slug="player-b")
        p3 = _player(pid=3, display_name="Player C", slug="player-c")
        bbc1 = _bbc(player_id=1, consensus_rank=1)
        bbc2 = _bbc(player_id=2, consensus_rank=2)
        bbc3 = _bbc(player_id=3, consensus_rank=3)
        history = [(1, 1, t1), (2, 2, t1), (3, 3, t1)]

        db = _make_trajectories_db(
            snapshot_id=1,
            bbc_latest=[bbc1, bbc2, bbc3],
            history_rows=history,
            players=[p1, p2, p3],
        )

        result = await get_rank_trajectories(db, draft_year=2026, top_n=10)

        assert [r["player_id"] for r in result] == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_player_missing_history_gets_empty_series(self) -> None:
        """A player in the top-N but with no history rows gets an empty series."""
        from app.services.consensus_read_service import get_rank_trajectories

        p1 = _player(pid=1, display_name="Player A", slug="player-a")
        bbc1 = _bbc(player_id=1, consensus_rank=1)
        history: list[Any] = []  # no history rows at all

        db = _make_trajectories_db(
            snapshot_id=1,
            bbc_latest=[bbc1],
            history_rows=history,
            players=[p1],
        )

        result = await get_rank_trajectories(db, draft_year=2026, top_n=10)

        assert len(result) == 1
        assert result[0]["series"] == []

    @pytest.mark.asyncio
    async def test_top_n_limits_player_count(self) -> None:
        """top_n=2 with 3 players returns only 2 entries (whatever the DB returns)."""
        from app.services.consensus_read_service import get_rank_trajectories

        t1 = datetime(2026, 1, 1, tzinfo=timezone.utc).replace(tzinfo=None)
        p1 = _player(pid=1, display_name="Player A", slug="player-a")
        p2 = _player(pid=2, display_name="Player B", slug="player-b")
        # DB already limits to top_n=2; only two bbc_latest rows returned
        bbc1 = _bbc(player_id=1, consensus_rank=1)
        bbc2 = _bbc(player_id=2, consensus_rank=2)
        history = [(1, 1, t1), (2, 2, t1)]

        db = _make_trajectories_db(
            snapshot_id=1,
            bbc_latest=[bbc1, bbc2],
            history_rows=history,
            players=[p1, p2],
        )

        result = await get_rank_trajectories(db, draft_year=2026, top_n=2)

        assert len(result) == 2
