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

from app.schemas.boards import (
    Board,
    BoardEntry,
    BoardKind,
    BoardStatus,
    ResolutionMethod,
)
from app.schemas.players_master import PlayerMaster


class BoardError(Exception):
    """Business-rule violation raised by the big board service."""


class BoardNotFoundError(BoardError):
    """Raised when a board id does not resolve to a row."""


class EntryNotFoundError(BoardError):
    """Raised when an entry id does not resolve to a row on the given board."""


class BoardNotEditableError(BoardError):
    """Raised when a mutation targets a board that is no longer PENDING."""


class DuplicatePositionError(BoardError):
    """Raised when a board already contains the requested rank."""


class DuplicatePlayerError(BoardError):
    """Raised when a board already contains the requested player."""


class BoardKindMismatchError(BoardError):
    """Raised when ``kind`` and ``num_rounds`` violate the kind-shape rule.

    Mirrors the ``ck_boards_kind_num_rounds`` DB CheckConstraint at the
    service layer so callers get a clean business error instead of an
    opaque ``IntegrityError`` on flush: a ``MOCK_DRAFT`` board must carry
    ``num_rounds``; a ``BIG_BOARD`` must not.
    """


class EntryAlreadyResolvedError(BoardError):
    """Raised when a resolution action targets an already-resolved entry.

    Guards irreversible actions (e.g. minting a stub) against stale pages
    or double-submits that would otherwise orphan a new row and clobber an
    existing resolution.
    """


@dataclass(frozen=True)
class EntryInput:
    """One ranked player passed in during board creation or row addition.

    ``player_id`` is optional: an entry can land unresolved (no DB match for
    the analyst's name) and a human resolves it later through the admin UI.

    ``vector_candidates`` is only populated for UNRESOLVED entries that went
    through the vector search step; it carries the top-K nearest-neighbour
    candidates as ``[{player_id, display_name, score}, ...]`` for admin review.
    """

    player_id: Optional[int]
    position: int
    raw_name: str = ""
    resolution_method: ResolutionMethod = ResolutionMethod.MANUAL
    tier: Optional[int] = None
    vector_candidates: Optional[list[dict]] = None  # type: ignore[type-arg]


async def create_board(
    db: AsyncSession,
    *,
    news_source_id: int,
    draft_year: int,
    published_at: datetime,
    entries: Sequence[EntryInput],
    news_item_id: Optional[int] = None,
    kind: BoardKind = BoardKind.BIG_BOARD,
    num_rounds: Optional[int] = None,
) -> Board:
    """Create a PENDING board with its initial entries.

    Args:
        db: Async session; caller owns commit.
        news_source_id: FK to the analyst/outlet that published this board.
        draft_year: Draft year the board ranks prospects for.
        published_at: When the analyst published the board.
        entries: Initial set of ranked players. May be empty if the admin
            wants to create the shell first and add rows afterward.
        news_item_id: Optional FK linking to the source article.
        kind: Board discriminator. Defaults to ``BIG_BOARD`` because that
            is the only kind with an active write path today; the column's
            server_default matches, so this is also a defense-in-depth
            against environments where the default isn't applied.
        num_rounds: Number of draft rounds a ``MOCK_DRAFT`` projects. Must
            be ``None`` for a ``BIG_BOARD`` and non-``None`` for a
            ``MOCK_DRAFT`` — see :class:`BoardKindMismatchError`. Note that
            the consensus engine ignores this field entirely; ``position``
            is the only ranking signal it reads, so a mock draft feeds the
            same consensus pool as a big board regardless of ``num_rounds``.

    Returns:
        The persisted Board (with ``id``, ``status`` defaulted to PENDING).

    Raises:
        BoardKindMismatchError: If ``kind`` and ``num_rounds`` violate the
            kind-shape rule (checked before any write).
        DuplicatePositionError / DuplicatePlayerError: If ``entries`` violates
            the per-board uniqueness constraints.
    """
    _validate_kind_num_rounds(kind, num_rounds)
    board = Board(
        kind=kind,
        num_rounds=num_rounds,
        news_source_id=news_source_id,
        news_item_id=news_item_id,
        draft_year=draft_year,
        published_at=published_at,
        size=len(entries),
        status=BoardStatus.PENDING,
    )
    db.add(board)
    await db.flush()
    assert board.id is not None  # populated by flush

    for entry in entries:
        db.add(
            BoardEntry(
                board_id=board.id,
                player_id=entry.player_id,
                position=entry.position,
                raw_name=entry.raw_name,
                resolution_method=entry.resolution_method,
                tier=entry.tier,
                vector_candidates=entry.vector_candidates,
            )
        )
    try:
        await db.flush()
    except IntegrityError as exc:
        _translate_entry_integrity_error(exc)

    return board


async def get_board(db: AsyncSession, board_id: int) -> Board:
    """Return the board or raise ``BoardNotFoundError``."""
    board = await db.get(Board, board_id)
    if board is None:
        raise BoardNotFoundError(f"No big board with id={board_id}")
    return board


async def get_board_with_entries(
    db: AsyncSession, board_id: int
) -> tuple[Board, list[BoardEntry]]:
    """Return ``(board, entries)`` ordered by rank ascending."""
    board = await get_board(db, board_id)
    result = await db.execute(
        select(BoardEntry)  # type: ignore[call-overload]
        .where(BoardEntry.board_id == board_id)  # type: ignore[arg-type]
        .order_by(BoardEntry.position)  # type: ignore[arg-type]
    )
    return board, list(result.scalars().all())


async def list_boards(
    db: AsyncSession,
    *,
    status: Optional[BoardStatus] = None,
    kind: Optional[BoardKind] = None,
    news_source_id: Optional[int] = None,
    draft_year: Optional[int] = None,
) -> list[Board]:
    """List boards filtered by any combination of status, kind, source, year.

    Ordering for PENDING boards: ``published_at`` DESC so the most recently
    published boards (which are also the ones admins should review first)
    appear at the top.  Non-PENDING boards also order by ``published_at``
    DESC.  (``confidence_score`` is not yet a column on ``Board``; this
    comment documents the intended future sort key for the approval queue.)
    """
    stmt = select(Board)  # type: ignore[call-overload]
    if status is not None:
        stmt = stmt.where(Board.status == status)  # type: ignore[arg-type]
    if kind is not None:
        stmt = stmt.where(Board.kind == kind)  # type: ignore[arg-type]
    if news_source_id is not None:
        stmt = stmt.where(Board.news_source_id == news_source_id)  # type: ignore[arg-type]
    if draft_year is not None:
        stmt = stmt.where(Board.draft_year == draft_year)  # type: ignore[arg-type]
    stmt = stmt.order_by(Board.published_at.desc())  # type: ignore[attr-defined]
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def add_entry(
    db: AsyncSession,
    *,
    board_id: int,
    player_id: Optional[int],
    position: int,
    raw_name: str = "",
    resolution_method: ResolutionMethod = ResolutionMethod.MANUAL,
    tier: Optional[int] = None,
    round: Optional[int] = None,
    team_id: Optional[int] = None,
    original_team_id: Optional[int] = None,
    trade_note: Optional[str] = None,
) -> BoardEntry:
    """Append an entry to a PENDING board and bump ``size``.

    Args:
        db: Async session; caller owns commit.
        board_id: PK of the parent :class:`Board`.
        player_id: Resolved player; may be ``None`` for unresolved entries.
        position: Rank (big board) or overall pick number (mock draft).
        raw_name: Verbatim analyst name before resolution.
        resolution_method: How the name was (or was not) resolved.
        tier: BIG_BOARD only — optional tier grouping.
        round: MOCK_DRAFT only — draft round (1 or 2).
        team_id: MOCK_DRAFT only — selecting team FK.
        original_team_id: MOCK_DRAFT only — original pick owner if traded.
        trade_note: MOCK_DRAFT only — free-text trade note.
    """
    board = await get_board(db, board_id)
    _require_pending(board)

    entry = BoardEntry(
        board_id=board_id,
        player_id=player_id,
        position=position,
        raw_name=raw_name,
        resolution_method=resolution_method,
        tier=tier,
        round=round,
        team_id=team_id,
        original_team_id=original_team_id,
        trade_note=trade_note,
    )
    db.add(entry)
    board.size = board.size + 1
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
    position: Optional[int] = None,
    tier: Optional[int] = None,
    round: Optional[int] = None,
    team_id: Optional[int] = None,
    original_team_id: Optional[int] = None,
    trade_note: Optional[str] = None,
) -> BoardEntry:
    """Patch a single entry on a PENDING board.

    Pass ``None`` to leave a field unchanged. ``tier`` is set verbatim,
    including to ``None`` if explicitly cleared via ``update_entry(...,
    tier=None)`` — callers that mean "don't touch tier" should omit it.
    Mock-draft fields (``round``, ``team_id``, ``original_team_id``,
    ``trade_note``) follow the same pass-``None``-to-leave-unchanged
    semantics; pass the sentinel string ``""`` at the call site if you
    want to explicitly clear ``trade_note``.
    """
    entry = await db.get(BoardEntry, entry_id)
    if entry is None:
        raise EntryNotFoundError(f"No board entry with id={entry_id}")

    board = await get_board(db, entry.board_id)
    _require_pending(board)

    if player_id is not None:
        entry.player_id = player_id
    if position is not None:
        entry.position = position
    if tier is not None:
        entry.tier = tier
    if round is not None:
        entry.round = round
    if team_id is not None:
        entry.team_id = team_id
    if original_team_id is not None:
        entry.original_team_id = original_team_id
    if trade_note is not None:
        entry.trade_note = trade_note or None  # collapse "" → None
    board.updated_at = datetime.utcnow()
    try:
        await db.flush()
    except IntegrityError as exc:
        _translate_entry_integrity_error(exc)
    return entry


async def assign_entry(
    db: AsyncSession,
    *,
    entry_id: int,
    player_id: int,
) -> BoardEntry:
    """Manually assign a player to an UNRESOLVED (or any PENDING) entry.

    Sets ``player_id`` to the supplied value and stamps
    ``resolution_method=MANUAL``.  The board must be PENDING; callers own
    the transaction.

    Args:
        db: Async session; caller owns commit.
        entry_id: PK of the ``board_entries`` row to update.
        player_id: PK of the ``players_master`` row to assign.

    Returns:
        The updated :class:`BoardEntry`.

    Raises:
        EntryNotFoundError: If ``entry_id`` does not resolve to a row.
        BoardNotEditableError: If the parent board is not PENDING.
        DuplicatePlayerError: If the board already contains this player on
            another entry.
    """
    entry = await db.get(BoardEntry, entry_id)
    if entry is None:
        raise EntryNotFoundError(f"No board entry with id={entry_id}")

    board = await get_board(db, entry.board_id)
    _require_pending(board)

    entry.player_id = player_id
    entry.resolution_method = ResolutionMethod.MANUAL
    board.updated_at = datetime.utcnow()
    try:
        await db.flush()
    except IntegrityError as exc:
        _translate_entry_integrity_error(exc)
    return entry


async def mint_stub_for_entry(
    db: AsyncSession,
    *,
    entry_id: int,
) -> BoardEntry:
    """Create a stub PlayerMaster from an unresolved entry's raw_name.

    Mints a new ``PlayerMaster`` row with ``is_stub=True`` and
    ``display_name=raw_name``, then assigns it to the entry with
    ``resolution_method=STUB``.  The ``before_insert`` listener
    auto-generates the slug; the ``after_commit`` hook queues an
    embedding.  The board must be PENDING; callers own the transaction.

    Args:
        db: Async session; caller owns commit.
        entry_id: PK of the ``board_entries`` row to resolve.

    Returns:
        The updated :class:`BoardEntry` (with ``player_id`` and method set).

    Raises:
        EntryNotFoundError: If ``entry_id`` does not resolve to a row.
        BoardNotEditableError: If the parent board is not PENDING.
        EntryAlreadyResolvedError: If the entry is already resolved (guards
            against stale-page/double-submit minting a duplicate stub).
        DuplicatePlayerError: If the board already contains a player
            assigned to this entry (e.g., a concurrent assign raced ahead).
    """
    entry = await db.get(BoardEntry, entry_id)
    if entry is None:
        raise EntryNotFoundError(f"No board entry with id={entry_id}")

    board = await get_board(db, entry.board_id)
    _require_pending(board)

    # Reject already-resolved entries: minting is irreversible, so a stale
    # page or double-submit must not orphan a second stub or clobber an
    # existing manual/candidate/exact resolution.
    if entry.player_id is not None or entry.resolution_method != (
        ResolutionMethod.UNRESOLVED
    ):
        raise EntryAlreadyResolvedError(
            f"Entry {entry_id} is already resolved "
            f"(method={entry.resolution_method.value}); refusing to mint a stub."
        )

    stub = PlayerMaster(
        display_name=entry.raw_name or f"Unknown #{entry_id}",
        is_stub=True,
    )
    db.add(stub)
    await db.flush()
    assert stub.id is not None

    entry.player_id = stub.id
    entry.resolution_method = ResolutionMethod.STUB
    board.updated_at = datetime.utcnow()
    try:
        await db.flush()
    except IntegrityError as exc:
        _translate_entry_integrity_error(exc)
    return entry


async def delete_entry(db: AsyncSession, *, entry_id: int) -> None:
    """Remove an entry from a PENDING board and decrement ``size``."""
    entry = await db.get(BoardEntry, entry_id)
    if entry is None:
        raise EntryNotFoundError(f"No big board entry with id={entry_id}")

    board = await get_board(db, entry.board_id)
    _require_pending(board)

    await db.delete(entry)
    board.size = max(0, board.size - 1)
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


async def approve_board(
    db: AsyncSession,
    *,
    board_id: int,
    recompute_consensus: bool = True,
) -> Board:
    """Transition a PENDING board to APPROVED and stamp ``approved_at``.

    When ``recompute_consensus`` is True (the default), this also kicks
    off a fresh consensus snapshot for the board's ``draft_year`` so
    the rest of the app sees up-to-date rankings. The recompute shares
    the caller's transaction — if it fails, the approval rolls back.
    Tests/admin paths that don't want this side-effect can pass False.
    """
    board = await get_board(db, board_id)
    _require_pending(board)
    now = datetime.utcnow()
    board.status = BoardStatus.APPROVED
    board.approved_at = now
    board.updated_at = now
    await db.flush()

    if recompute_consensus:
        # Local import to avoid a top-of-module dependency cycle between
        # board_service and consensus_service (consensus_service
        # already imports the Board schema).
        from app.services.consensus_service import (
            ConsensusTrigger,
            recompute_consensus as _recompute,
        )

        await _recompute(
            db, draft_year=board.draft_year, trigger=ConsensusTrigger.BOARD_APPROVED
        )

    return board


async def reject_board(db: AsyncSession, *, board_id: int) -> Board:
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


async def update_board_metadata(
    db: AsyncSession,
    *,
    board_id: int,
    news_source_id: Optional[int] = None,
    draft_year: Optional[int] = None,
    published_at: Optional[datetime] = None,
) -> Board:
    """Patch source / draft year / published_at on a PENDING board.

    Pass ``None`` to leave a field unchanged. Only PENDING boards may
    be edited; APPROVED and REJECTED stay locked so their attribution
    can't drift after the fact.
    """
    board = await get_board(db, board_id)
    _require_pending(board)

    if news_source_id is not None:
        board.news_source_id = news_source_id
    if draft_year is not None:
        board.draft_year = draft_year
    if published_at is not None:
        board.published_at = published_at
    board.updated_at = datetime.utcnow()
    await db.flush()
    return board


async def reopen_board(db: AsyncSession, *, board_id: int) -> Board:
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
) -> Board:
    """Create a fresh PENDING board with the same entries as ``board_id``.

    Useful when the next published board from a source is mostly the
    same lineup as the previous one — clone, then tweak ranks/players.
    The new board's source, draft_year, and entry list match the source
    board; ``published_at`` is provided by the caller; status is PENDING.
    """
    source_board, entries = await get_board_with_entries(db, board_id)
    clone = Board(
        news_source_id=source_board.news_source_id,
        news_item_id=None,
        draft_year=source_board.draft_year,
        published_at=published_at,
        size=len(entries),
        status=BoardStatus.PENDING,
    )
    db.add(clone)
    await db.flush()
    assert clone.id is not None

    for entry in entries:
        db.add(
            BoardEntry(
                board_id=clone.id,
                player_id=entry.player_id,
                position=entry.position,
                raw_name=entry.raw_name,
                resolution_method=entry.resolution_method,
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
) -> BoardEntry:
    """Swap an entry's rank with its neighbor above (``up``) or below (``down``).

    No-op when the entry is already at the top (for ``up``) or bottom
    (for ``down``) of the board. Uses a temporary negative rank between
    the two updates so the unique ``(board_id, rank)`` constraint is
    satisfied at every flush.
    """
    if direction not in {"up", "down"}:
        raise BoardError(f"Invalid direction: {direction!r}")

    entry = await db.get(BoardEntry, entry_id)
    if entry is None:
        raise EntryNotFoundError(f"No big board entry with id={entry_id}")

    board = await get_board(db, entry.board_id)
    _require_pending(board)

    neighbor_rank = entry.position - 1 if direction == "up" else entry.position + 1
    if neighbor_rank < 1:
        return entry  # already at top

    result = await db.execute(
        select(BoardEntry)  # type: ignore[call-overload]
        .where(BoardEntry.board_id == board.id)  # type: ignore[arg-type]
        .where(BoardEntry.position == neighbor_rank)  # type: ignore[arg-type]
    )
    neighbor = result.scalar_one_or_none()
    if neighbor is None:
        return entry  # no neighbor in that direction (entry is at bottom)

    # Two-step swap so we never violate uq(board_id, rank).
    original = entry.position
    entry.position = -original  # sentinel; never collides with a real 1-based rank
    await db.flush()
    neighbor.position = original
    await db.flush()
    entry.position = neighbor_rank
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
        select(BoardEntry.tier)  # type: ignore[call-overload]
        .where(BoardEntry.board_id == board_id)  # type: ignore[arg-type]
        .order_by(BoardEntry.position.desc())  # type: ignore[attr-defined]
        .limit(1)
    )
    row = result.first()
    return row[0] if row else None


def _validate_kind_num_rounds(kind: BoardKind, num_rounds: Optional[int]) -> None:
    """Enforce the kind-shape rule before persisting (see CheckConstraint).

    A ``MOCK_DRAFT`` must carry ``num_rounds``; a ``BIG_BOARD`` must not.
    Raising here turns an opaque DB ``IntegrityError`` into a clear,
    catchable business error for routes and the extraction service.
    """
    if kind is BoardKind.MOCK_DRAFT and num_rounds is None:
        raise BoardKindMismatchError(
            "A MOCK_DRAFT board requires num_rounds (got None)."
        )
    if kind is BoardKind.BIG_BOARD and num_rounds is not None:
        raise BoardKindMismatchError(
            f"A BIG_BOARD board must not set num_rounds (got {num_rounds})."
        )


def _require_pending(board: Board) -> None:
    if board.status is not BoardStatus.PENDING:
        raise BoardNotEditableError(
            f"Board {board.id} is {board.status.value}; only PENDING boards may be modified."
        )


def _translate_entry_integrity_error(exc: IntegrityError) -> None:
    """Map DB unique-constraint violations to typed service errors."""
    message = str(exc.orig) if exc.orig else str(exc)
    if "uq_board_entries_board_position" in message:
        raise DuplicatePositionError(
            "This board already has an entry at that rank."
        ) from exc
    if "uq_board_entries_board_player" in message:
        raise DuplicatePlayerError("This board already includes that player.") from exc
    raise BoardError(message) from exc
