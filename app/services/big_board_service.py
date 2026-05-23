"""Service layer for big board CRUD and approval lifecycle.

Boards move through PENDING -> APPROVED or PENDING -> REJECTED. Only PENDING
boards may be edited or deleted; APPROVED and REJECTED rows are immutable
so they preserve a faithful record of the analyst's rankings (and the
admin's review decision) for downstream consensus computation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.big_boards import BigBoard, BigBoardEntry, BoardStatus


class BigBoardError(Exception):
    """Business-rule violation raised by the big board service."""


class BoardNotFoundError(BigBoardError):
    """Raised when a board id does not resolve to a row."""


class EntryNotFoundError(BigBoardError):
    """Raised when an entry id does not resolve to a row on the given board."""


class BoardNotEditableError(BigBoardError):
    """Raised when a mutation targets a board that is no longer PENDING."""


class DuplicateRankError(BigBoardError):
    """Raised when a board already contains the requested rank."""


class DuplicatePlayerError(BigBoardError):
    """Raised when a board already contains the requested player."""


@dataclass(frozen=True)
class EntryInput:
    """One ranked player passed in during board creation or row addition."""

    player_id: int
    rank: int
    tier: Optional[int] = None


async def create_board(
    db: AsyncSession,
    *,
    news_source_id: int,
    draft_year: int,
    published_at: datetime,
    entries: Sequence[EntryInput],
    news_item_id: Optional[int] = None,
) -> BigBoard:
    """Create a PENDING big board with its initial entries.

    Args:
        db: Async session; caller owns commit.
        news_source_id: FK to the analyst/outlet that published this board.
        draft_year: Draft year the board ranks prospects for.
        published_at: When the analyst published the board.
        entries: Initial set of ranked players. May be empty if the admin
            wants to create the shell first and add rows afterward.
        news_item_id: Optional FK linking to the source article.

    Returns:
        The persisted BigBoard (with ``id``, ``status`` defaulted to PENDING).

    Raises:
        DuplicateRankError / DuplicatePlayerError: If ``entries`` violates
            the per-board uniqueness constraints.
    """
    board = BigBoard(
        news_source_id=news_source_id,
        news_item_id=news_item_id,
        draft_year=draft_year,
        published_at=published_at,
        board_size=len(entries),
        status=BoardStatus.PENDING,
    )
    db.add(board)
    await db.flush()
    assert board.id is not None  # populated by flush

    for entry in entries:
        db.add(
            BigBoardEntry(
                board_id=board.id,
                player_id=entry.player_id,
                rank=entry.rank,
                tier=entry.tier,
            )
        )
    try:
        await db.flush()
    except IntegrityError as exc:
        _translate_entry_integrity_error(exc)

    return board


async def get_board(db: AsyncSession, board_id: int) -> BigBoard:
    """Return the board or raise ``BoardNotFoundError``."""
    board = await db.get(BigBoard, board_id)
    if board is None:
        raise BoardNotFoundError(f"No big board with id={board_id}")
    return board


async def get_board_with_entries(
    db: AsyncSession, board_id: int
) -> tuple[BigBoard, list[BigBoardEntry]]:
    """Return ``(board, entries)`` ordered by rank ascending."""
    board = await get_board(db, board_id)
    result = await db.execute(
        select(BigBoardEntry)  # type: ignore[call-overload]
        .where(BigBoardEntry.board_id == board_id)  # type: ignore[arg-type]
        .order_by(BigBoardEntry.rank)  # type: ignore[arg-type]
    )
    return board, list(result.scalars().all())


async def list_boards(
    db: AsyncSession,
    *,
    status: Optional[BoardStatus] = None,
    news_source_id: Optional[int] = None,
    draft_year: Optional[int] = None,
) -> list[BigBoard]:
    """List boards filtered by any combination of status, source, year.

    Results ordered by ``published_at`` desc so the most recent boards
    appear first.
    """
    stmt = select(BigBoard)  # type: ignore[call-overload]
    if status is not None:
        stmt = stmt.where(BigBoard.status == status)  # type: ignore[arg-type]
    if news_source_id is not None:
        stmt = stmt.where(BigBoard.news_source_id == news_source_id)  # type: ignore[arg-type]
    if draft_year is not None:
        stmt = stmt.where(BigBoard.draft_year == draft_year)  # type: ignore[arg-type]
    stmt = stmt.order_by(BigBoard.published_at.desc())  # type: ignore[attr-defined]
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def add_entry(
    db: AsyncSession,
    *,
    board_id: int,
    player_id: int,
    rank: int,
    tier: Optional[int] = None,
) -> BigBoardEntry:
    """Append an entry to a PENDING board and bump ``board_size``."""
    board = await get_board(db, board_id)
    _require_pending(board)

    entry = BigBoardEntry(board_id=board_id, player_id=player_id, rank=rank, tier=tier)
    db.add(entry)
    board.board_size = board.board_size + 1
    board.updated_at = datetime.utcnow()
    try:
        await db.flush()
    except IntegrityError as exc:
        _translate_entry_integrity_error(exc)
    return entry


async def update_entry(
    db: AsyncSession,
    *,
    entry_id: int,
    player_id: Optional[int] = None,
    rank: Optional[int] = None,
    tier: Optional[int] = None,
) -> BigBoardEntry:
    """Patch a single entry on a PENDING board.

    Pass ``None`` to leave a field unchanged. ``tier`` is set verbatim,
    including to ``None`` if explicitly cleared via ``update_entry(...,
    tier=None)`` — callers that mean "don't touch tier" should omit it.
    """
    entry = await db.get(BigBoardEntry, entry_id)
    if entry is None:
        raise EntryNotFoundError(f"No big board entry with id={entry_id}")

    board = await get_board(db, entry.board_id)
    _require_pending(board)

    if player_id is not None:
        entry.player_id = player_id
    if rank is not None:
        entry.rank = rank
    if tier is not None:
        entry.tier = tier
    board.updated_at = datetime.utcnow()
    try:
        await db.flush()
    except IntegrityError as exc:
        _translate_entry_integrity_error(exc)
    return entry


async def delete_entry(db: AsyncSession, *, entry_id: int) -> None:
    """Remove an entry from a PENDING board and decrement ``board_size``."""
    entry = await db.get(BigBoardEntry, entry_id)
    if entry is None:
        raise EntryNotFoundError(f"No big board entry with id={entry_id}")

    board = await get_board(db, entry.board_id)
    _require_pending(board)

    await db.delete(entry)
    board.board_size = max(0, board.board_size - 1)
    board.updated_at = datetime.utcnow()
    await db.flush()


async def delete_board(db: AsyncSession, *, board_id: int) -> None:
    """Hard-delete a PENDING board (entries cascade).

    Reserved for typos / accidental boards. Use ``reject_board`` instead
    when the audit trail should record that a real submission was
    refused.
    """
    board = await get_board(db, board_id)
    _require_pending(board)
    await db.delete(board)
    await db.flush()


async def approve_board(db: AsyncSession, *, board_id: int) -> BigBoard:
    """Transition a PENDING board to APPROVED and stamp ``approved_at``."""
    board = await get_board(db, board_id)
    _require_pending(board)
    now = datetime.utcnow()
    board.status = BoardStatus.APPROVED
    board.approved_at = now
    board.updated_at = now
    await db.flush()
    return board


async def reject_board(db: AsyncSession, *, board_id: int) -> BigBoard:
    """Transition a PENDING board to REJECTED.

    The row is preserved so the audit trail records that the analyst's
    submission was reviewed and refused. ``approved_at`` stays ``None``.
    """
    board = await get_board(db, board_id)
    _require_pending(board)
    board.status = BoardStatus.REJECTED
    board.updated_at = datetime.utcnow()
    await db.flush()
    return board


async def reopen_board(db: AsyncSession, *, board_id: int) -> BigBoard:
    """Unlock an APPROVED board back to PENDING so it can be edited.

    Clears ``approved_at`` since the prior approval is no longer current.
    Only APPROVED boards may be reopened; REJECTED boards stay locked.
    """
    board = await get_board(db, board_id)
    if board.status is not BoardStatus.APPROVED:
        raise BoardNotEditableError(
            f"Board {board.id} is {board.status.value}; only APPROVED boards may be reopened."
        )
    board.status = BoardStatus.PENDING
    board.approved_at = None
    board.updated_at = datetime.utcnow()
    await db.flush()
    return board


async def clone_board(
    db: AsyncSession,
    *,
    board_id: int,
    published_at: datetime,
) -> BigBoard:
    """Create a fresh PENDING board with the same entries as ``board_id``.

    Useful when the next published board from a source is mostly the
    same lineup as the previous one — clone, then tweak ranks/players.
    The new board's source, draft_year, and entry list match the source
    board; ``published_at`` is provided by the caller; status is PENDING.
    """
    source_board, entries = await get_board_with_entries(db, board_id)
    clone = BigBoard(
        news_source_id=source_board.news_source_id,
        news_item_id=None,
        draft_year=source_board.draft_year,
        published_at=published_at,
        board_size=len(entries),
        status=BoardStatus.PENDING,
    )
    db.add(clone)
    await db.flush()
    assert clone.id is not None

    for entry in entries:
        db.add(
            BigBoardEntry(
                board_id=clone.id,
                player_id=entry.player_id,
                rank=entry.rank,
                tier=entry.tier,
            )
        )
    try:
        await db.flush()
    except IntegrityError as exc:
        _translate_entry_integrity_error(exc)
    return clone


async def move_entry(
    db: AsyncSession,
    *,
    entry_id: int,
    direction: str,
) -> BigBoardEntry:
    """Swap an entry's rank with its neighbor above (``up``) or below (``down``).

    No-op when the entry is already at the top (for ``up``) or bottom
    (for ``down``) of the board. Uses a temporary negative rank between
    the two updates so the unique ``(board_id, rank)`` constraint is
    satisfied at every flush.
    """
    if direction not in {"up", "down"}:
        raise BigBoardError(f"Invalid direction: {direction!r}")

    entry = await db.get(BigBoardEntry, entry_id)
    if entry is None:
        raise EntryNotFoundError(f"No big board entry with id={entry_id}")

    board = await get_board(db, entry.board_id)
    _require_pending(board)

    neighbor_rank = entry.rank - 1 if direction == "up" else entry.rank + 1
    if neighbor_rank < 1:
        return entry  # already at top

    result = await db.execute(
        select(BigBoardEntry)  # type: ignore[call-overload]
        .where(BigBoardEntry.board_id == board.id)  # type: ignore[arg-type]
        .where(BigBoardEntry.rank == neighbor_rank)  # type: ignore[arg-type]
    )
    neighbor = result.scalar_one_or_none()
    if neighbor is None:
        return entry  # no neighbor in that direction (entry is at bottom)

    # Two-step swap so we never violate uq(board_id, rank).
    original = entry.rank
    entry.rank = -original  # sentinel; never collides with a real 1-based rank
    await db.flush()
    neighbor.rank = original
    await db.flush()
    entry.rank = neighbor_rank
    board.updated_at = datetime.utcnow()
    await db.flush()
    return entry


async def latest_entry_tier(db: AsyncSession, *, board_id: int) -> Optional[int]:
    """Return the tier of the highest-rank (most-recently-added) entry, if any.

    Used by the admin UI to pre-fill the add form's tier input so an
    admin doesn't have to re-type ``tier=1`` for every player in a board
    that is mostly tier 1.
    """
    result = await db.execute(
        select(BigBoardEntry.tier)  # type: ignore[call-overload]
        .where(BigBoardEntry.board_id == board_id)  # type: ignore[arg-type]
        .order_by(BigBoardEntry.rank.desc())  # type: ignore[attr-defined]
        .limit(1)
    )
    row = result.first()
    return row[0] if row else None


def _require_pending(board: BigBoard) -> None:
    if board.status is not BoardStatus.PENDING:
        raise BoardNotEditableError(
            f"Board {board.id} is {board.status.value}; only PENDING boards may be modified."
        )


def _translate_entry_integrity_error(exc: IntegrityError) -> None:
    """Map DB unique-constraint violations to typed service errors."""
    message = str(exc.orig) if exc.orig else str(exc)
    if "uq_big_board_entries_board_rank" in message:
        raise DuplicateRankError(
            "This board already has an entry at that rank."
        ) from exc
    if "uq_big_board_entries_board_player" in message:
        raise DuplicatePlayerError("This board already includes that player.") from exc
    raise BigBoardError(message) from exc
