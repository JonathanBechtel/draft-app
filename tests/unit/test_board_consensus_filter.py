"""Unit tests for the pure consensus-role helpers in board_service.

No DB, no network. Boards are built as the real SQLModel class with only
the fields the helpers touch (``id`` and ``status``).
"""

from __future__ import annotations

import pytest

from app.schemas.boards import Board, BoardStatus
from app.services.board_service import (
    filter_boards_by_consensus_role,
    normalize_consensus_role,
)


def _board(board_id: int, status: BoardStatus) -> Board:
    """Minimal Board with just the fields the role helpers read."""
    return Board(id=board_id, status=status)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("live", "live"),
        ("superseded", "superseded"),
        ("", None),
        (None, None),
        ("LIVE", None),
        ("garbage", None),
    ],
)
def test_normalize_consensus_role(raw: str | None, expected: str | None):
    """Only the two known roles survive; everything else normalizes to None."""
    assert normalize_consensus_role(raw) == expected


def test_filter_live_keeps_only_snapshot_boards():
    """role='live' returns exactly the boards whose ids are in the live set."""
    boards = [
        _board(1, BoardStatus.APPROVED),
        _board(2, BoardStatus.APPROVED),
        _board(3, BoardStatus.PENDING),
    ]
    out = filter_boards_by_consensus_role(boards, live_board_ids={1, 3}, role="live")
    assert [b.id for b in out] == [1, 3]


def test_filter_superseded_keeps_only_replaced_approved_boards():
    """role='superseded' = APPROVED boards absent from the live set.

    Pending/rejected boards are excluded even when not live, and live
    approved boards are excluded because they are not superseded.
    """
    boards = [
        _board(1, BoardStatus.APPROVED),  # live -> excluded
        _board(2, BoardStatus.APPROVED),  # approved, not live -> kept
        _board(3, BoardStatus.PENDING),  # not approved -> excluded
        _board(4, BoardStatus.REJECTED),  # not approved -> excluded
    ]
    out = filter_boards_by_consensus_role(boards, live_board_ids={1}, role="superseded")
    assert [b.id for b in out] == [2]


def test_filter_none_returns_unchanged_copy():
    """role=None returns every board, as a new list (input not mutated)."""
    boards = [_board(1, BoardStatus.APPROVED), _board(2, BoardStatus.PENDING)]
    out = filter_boards_by_consensus_role(boards, live_board_ids=set(), role=None)
    assert [b.id for b in out] == [1, 2]
    assert out is not boards
