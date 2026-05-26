"""Integration tests for the big board service layer.

These exercise the service against a live test DB session so the unique
constraints and status guards behave the way the routes will rely on.
"""

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.boards import (
    Board,
    BoardEntry,
    BoardKind,
    BoardStatus,
    ResolutionMethod,
)
from app.schemas.news_sources import NewsSource
from app.schemas.players_master import PlayerMaster
from app.services import board_service as svc
from tests.integration.conftest import make_player


@pytest_asyncio.fixture()
async def players(db_session: AsyncSession) -> list[PlayerMaster]:
    """Three persisted players we can rank on a board."""
    rows = [
        make_player("Cooper", "Flagg", school="Duke"),
        make_player("Dylan", "Harper", school="Rutgers"),
        make_player("Ace", "Bailey", school="Rutgers"),
    ]
    for p in rows:
        db_session.add(p)
    await db_session.flush()
    return rows


def _published_at() -> datetime:
    """Naive UTC timestamp suitable for the schema's timestamp column."""
    return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)


@pytest.mark.asyncio
async def test_create_board_persists_pending_board_and_entries(
    db_session: AsyncSession,
    news_source: NewsSource,
    players: list[PlayerMaster],
) -> None:
    """create_board writes PENDING board + ordered entries; size matches."""
    assert news_source.id is not None
    board = await svc.create_board(
        db_session,
        news_source_id=news_source.id,
        draft_year=2026,
        published_at=_published_at(),
        entries=[
            svc.EntryInput(player_id=players[0].id, position=1, tier=1),  # type: ignore[arg-type]
            svc.EntryInput(player_id=players[1].id, position=2, tier=1),  # type: ignore[arg-type]
            svc.EntryInput(player_id=players[2].id, position=3, tier=2),  # type: ignore[arg-type]
        ],
    )
    await db_session.commit()

    assert board.status is BoardStatus.PENDING
    assert board.approved_at is None
    assert board.size == 3
    # Defaults to BIG_BOARD when caller omits ``kind`` — without this the
    # NOT NULL column would only land via the server-side default, which
    # has historically been missing on some environments. See #235.
    assert board.kind is BoardKind.BIG_BOARD

    _, entries = await svc.get_board_with_entries(db_session, board.id)  # type: ignore[arg-type]
    assert [(e.position, e.player_id, e.tier) for e in entries] == [
        (1, players[0].id, 1),
        (2, players[1].id, 1),
        (3, players[2].id, 2),
    ]


@pytest.mark.asyncio
async def test_create_board_persists_explicit_kind(
    db_session: AsyncSession,
    news_source: NewsSource,
    players: list[PlayerMaster],
) -> None:
    """Caller can override ``kind`` (forward-compat for mock-draft path)."""
    assert news_source.id is not None
    board = await svc.create_board(
        db_session,
        kind=BoardKind.BIG_BOARD,
        news_source_id=news_source.id,
        draft_year=2026,
        published_at=_published_at(),
        entries=[
            svc.EntryInput(player_id=players[0].id, position=1),  # type: ignore[arg-type]
        ],
    )
    await db_session.commit()

    refreshed = await db_session.get(Board, board.id)
    assert refreshed is not None
    assert refreshed.kind is BoardKind.BIG_BOARD


@pytest.mark.asyncio
async def test_create_board_rejects_duplicate_rank(
    db_session: AsyncSession,
    news_source: NewsSource,
    players: list[PlayerMaster],
) -> None:
    """Two entries at rank 1 on the same board raises DuplicatePositionError."""
    assert news_source.id is not None
    with pytest.raises(svc.DuplicatePositionError):
        await svc.create_board(
            db_session,
            news_source_id=news_source.id,
            draft_year=2026,
            published_at=_published_at(),
            entries=[
                svc.EntryInput(player_id=players[0].id, position=1),  # type: ignore[arg-type]
                svc.EntryInput(player_id=players[1].id, position=1),  # type: ignore[arg-type]
            ],
        )


@pytest.mark.asyncio
async def test_create_board_rejects_duplicate_player(
    db_session: AsyncSession,
    news_source: NewsSource,
    players: list[PlayerMaster],
) -> None:
    """Same player twice on the same board raises DuplicatePlayerError."""
    assert news_source.id is not None
    with pytest.raises(svc.DuplicatePlayerError):
        await svc.create_board(
            db_session,
            news_source_id=news_source.id,
            draft_year=2026,
            published_at=_published_at(),
            entries=[
                svc.EntryInput(player_id=players[0].id, position=1),  # type: ignore[arg-type]
                svc.EntryInput(player_id=players[0].id, position=2),  # type: ignore[arg-type]
            ],
        )


@pytest.mark.asyncio
async def test_add_update_delete_entry_on_pending_board(
    db_session: AsyncSession,
    news_source: NewsSource,
    players: list[PlayerMaster],
) -> None:
    """A PENDING board accepts add, update, and delete on its entries."""
    assert news_source.id is not None
    board = await svc.create_board(
        db_session,
        news_source_id=news_source.id,
        draft_year=2026,
        published_at=_published_at(),
        entries=[svc.EntryInput(player_id=players[0].id, position=1)],  # type: ignore[arg-type]
    )
    await db_session.commit()

    added = await svc.add_entry(
        db_session,
        board_id=board.id,  # type: ignore[arg-type]
        player_id=players[1].id,  # type: ignore[arg-type]
        position=2,
    )
    await db_session.commit()
    assert board.size == 2

    await svc.update_entry(
        db_session, entry_id=added.id, position=5, tier=3  # type: ignore[arg-type]
    )
    await db_session.commit()
    refreshed = await db_session.get(BoardEntry, added.id)
    assert refreshed is not None
    assert refreshed.position == 5
    assert refreshed.tier == 3

    await svc.delete_entry(db_session, entry_id=added.id)  # type: ignore[arg-type]
    await db_session.commit()
    assert await db_session.get(BoardEntry, added.id) is None
    refreshed_board = await db_session.get(Board, board.id)
    assert refreshed_board is not None
    assert refreshed_board.size == 1


@pytest.mark.asyncio
async def test_approved_board_is_immutable(
    db_session: AsyncSession,
    news_source: NewsSource,
    players: list[PlayerMaster],
) -> None:
    """After approval, every mutating service call raises BoardNotEditableError."""
    assert news_source.id is not None
    board = await svc.create_board(
        db_session,
        news_source_id=news_source.id,
        draft_year=2026,
        published_at=_published_at(),
        entries=[svc.EntryInput(player_id=players[0].id, position=1)],  # type: ignore[arg-type]
    )
    await db_session.commit()

    approved = await svc.approve_board(db_session, board_id=board.id)  # type: ignore[arg-type]
    await db_session.commit()
    assert approved.status is BoardStatus.APPROVED
    assert approved.approved_at is not None

    entry_id = (
        await db_session.execute(
            select(BoardEntry.id).where(  # type: ignore[call-overload]
                BoardEntry.board_id == board.id  # type: ignore[arg-type]
            )
        )
    ).scalar_one()

    with pytest.raises(svc.BoardNotEditableError):
        await svc.add_entry(
            db_session,
            board_id=board.id,  # type: ignore[arg-type]
            player_id=players[1].id,  # type: ignore[arg-type]
            position=2,
        )
    with pytest.raises(svc.BoardNotEditableError):
        await svc.update_entry(db_session, entry_id=entry_id, position=99)
    with pytest.raises(svc.BoardNotEditableError):
        await svc.delete_entry(db_session, entry_id=entry_id)
    with pytest.raises(svc.BoardNotEditableError):
        await svc.delete_board(db_session, board_id=board.id)  # type: ignore[arg-type]
    with pytest.raises(svc.BoardNotEditableError):
        await svc.approve_board(db_session, board_id=board.id)  # type: ignore[arg-type]
    with pytest.raises(svc.BoardNotEditableError):
        await svc.reject_board(db_session, board_id=board.id)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_rejected_board_is_immutable(
    db_session: AsyncSession,
    news_source: NewsSource,
    players: list[PlayerMaster],
) -> None:
    """Rejected boards are preserved as an audit record and locked from edits."""
    assert news_source.id is not None
    board = await svc.create_board(
        db_session,
        news_source_id=news_source.id,
        draft_year=2026,
        published_at=_published_at(),
        entries=[svc.EntryInput(player_id=players[0].id, position=1)],  # type: ignore[arg-type]
    )
    await db_session.commit()

    rejected = await svc.reject_board(db_session, board_id=board.id)  # type: ignore[arg-type]
    await db_session.commit()
    assert rejected.status is BoardStatus.REJECTED
    assert rejected.approved_at is None

    with pytest.raises(svc.BoardNotEditableError):
        await svc.approve_board(db_session, board_id=board.id)  # type: ignore[arg-type]
    with pytest.raises(svc.BoardNotEditableError):
        await svc.delete_board(db_session, board_id=board.id)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_delete_pending_board_cascades_entries(
    db_session: AsyncSession,
    news_source: NewsSource,
    players: list[PlayerMaster],
) -> None:
    """Hard-deleting a PENDING board removes its entries via the FK cascade."""
    assert news_source.id is not None
    board = await svc.create_board(
        db_session,
        news_source_id=news_source.id,
        draft_year=2026,
        published_at=_published_at(),
        entries=[
            svc.EntryInput(player_id=players[0].id, position=1),  # type: ignore[arg-type]
            svc.EntryInput(player_id=players[1].id, position=2),  # type: ignore[arg-type]
        ],
    )
    await db_session.commit()
    board_id = board.id

    await svc.delete_board(db_session, board_id=board_id)  # type: ignore[arg-type]
    await db_session.commit()

    assert await db_session.get(Board, board_id) is None
    remaining = (
        await db_session.execute(
            select(BoardEntry).where(  # type: ignore[call-overload]
                BoardEntry.board_id == board_id  # type: ignore[arg-type]
            )
        )
    ).scalars().all()
    assert remaining == []


@pytest.mark.asyncio
async def test_update_board_metadata_changes_source_year_published_at(
    db_session: AsyncSession,
    news_source: NewsSource,
    players: list[PlayerMaster],
) -> None:
    """update_board_metadata patches the editable fields on a PENDING board."""
    assert news_source.id is not None
    other_source = NewsSource(
        name="alt-source",
        display_name="Alt Source",
        feed_url="https://example.com/alt-feed",
    )
    db_session.add(other_source)
    await db_session.flush()
    assert other_source.id is not None

    board = await svc.create_board(
        db_session,
        news_source_id=news_source.id,
        draft_year=2026,
        published_at=_published_at(),
        entries=[svc.EntryInput(player_id=players[0].id, position=1)],  # type: ignore[arg-type]
    )
    await db_session.commit()

    new_published = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
        days=5
    )
    updated = await svc.update_board_metadata(
        db_session,
        board_id=board.id,  # type: ignore[arg-type]
        news_source_id=other_source.id,
        draft_year=2027,
        published_at=new_published,
    )
    await db_session.commit()

    assert updated.news_source_id == other_source.id
    assert updated.draft_year == 2027
    assert updated.published_at == new_published
    assert updated.status is BoardStatus.PENDING


@pytest.mark.asyncio
async def test_update_board_metadata_rejects_non_pending_board(
    db_session: AsyncSession,
    news_source: NewsSource,
    players: list[PlayerMaster],
) -> None:
    """Approved and rejected boards refuse metadata edits."""
    assert news_source.id is not None
    board = await svc.create_board(
        db_session,
        news_source_id=news_source.id,
        draft_year=2026,
        published_at=_published_at(),
        entries=[svc.EntryInput(player_id=players[0].id, position=1)],  # type: ignore[arg-type]
    )
    await db_session.commit()
    await svc.approve_board(db_session, board_id=board.id)  # type: ignore[arg-type]
    await db_session.commit()

    with pytest.raises(svc.BoardNotEditableError):
        await svc.update_board_metadata(
            db_session,
            board_id=board.id,  # type: ignore[arg-type]
            draft_year=2027,
        )


@pytest.mark.asyncio
async def test_reopen_board_flips_approved_to_pending(
    db_session: AsyncSession,
    news_source: NewsSource,
    players: list[PlayerMaster],
) -> None:
    """reopen_board sends APPROVED back to PENDING and clears approved_at."""
    assert news_source.id is not None
    board = await svc.create_board(
        db_session,
        news_source_id=news_source.id,
        draft_year=2026,
        published_at=_published_at(),
        entries=[svc.EntryInput(player_id=players[0].id, position=1)],  # type: ignore[arg-type]
    )
    await db_session.commit()
    await svc.approve_board(db_session, board_id=board.id)  # type: ignore[arg-type]
    await db_session.commit()
    assert board.status is BoardStatus.APPROVED
    assert board.approved_at is not None

    reopened = await svc.reopen_board(db_session, board_id=board.id)  # type: ignore[arg-type]
    await db_session.commit()
    assert reopened.status is BoardStatus.PENDING
    assert reopened.approved_at is None


@pytest.mark.asyncio
async def test_reopen_rejects_non_approved_board(
    db_session: AsyncSession,
    news_source: NewsSource,
    players: list[PlayerMaster],
) -> None:
    """Reopen only works on APPROVED; PENDING and REJECTED both raise."""
    assert news_source.id is not None
    pending = await svc.create_board(
        db_session,
        news_source_id=news_source.id,
        draft_year=2026,
        published_at=_published_at(),
        entries=[svc.EntryInput(player_id=players[0].id, position=1)],  # type: ignore[arg-type]
    )
    await db_session.commit()
    with pytest.raises(svc.BoardNotEditableError):
        await svc.reopen_board(db_session, board_id=pending.id)  # type: ignore[arg-type]

    rejected = await svc.create_board(
        db_session,
        news_source_id=news_source.id,
        draft_year=2026,
        published_at=_published_at() - timedelta(days=1),
        entries=[svc.EntryInput(player_id=players[1].id, position=1)],  # type: ignore[arg-type]
    )
    await db_session.commit()
    await svc.reject_board(db_session, board_id=rejected.id)  # type: ignore[arg-type]
    await db_session.commit()
    with pytest.raises(svc.BoardNotEditableError):
        await svc.reopen_board(db_session, board_id=rejected.id)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_clone_board_copies_entries_into_new_pending_board(
    db_session: AsyncSession,
    news_source: NewsSource,
    players: list[PlayerMaster],
) -> None:
    """Clone produces a PENDING board with the same source, year, and entries."""
    assert news_source.id is not None
    original = await svc.create_board(
        db_session,
        news_source_id=news_source.id,
        draft_year=2026,
        published_at=_published_at(),
        entries=[
            svc.EntryInput(player_id=players[0].id, position=1, tier=1),  # type: ignore[arg-type]
            svc.EntryInput(player_id=players[1].id, position=2, tier=1),  # type: ignore[arg-type]
            svc.EntryInput(player_id=players[2].id, position=3, tier=2),  # type: ignore[arg-type]
        ],
    )
    await svc.approve_board(db_session, board_id=original.id)  # type: ignore[arg-type]
    await db_session.commit()

    new_published = datetime.now(timezone.utc).replace(tzinfo=None)
    clone = await svc.clone_board(
        db_session, board_id=original.id, published_at=new_published  # type: ignore[arg-type]
    )
    await db_session.commit()

    assert clone.id != original.id
    assert clone.status is BoardStatus.PENDING
    assert clone.news_source_id == original.news_source_id
    assert clone.draft_year == original.draft_year
    assert clone.size == 3
    assert clone.published_at == new_published

    _, clone_entries = await svc.get_board_with_entries(db_session, clone.id)  # type: ignore[arg-type]
    assert [(e.position, e.player_id, e.tier) for e in clone_entries] == [
        (1, players[0].id, 1),
        (2, players[1].id, 1),
        (3, players[2].id, 2),
    ]


async def _ranks_by_entry_id(
    db: AsyncSession, board_id: int
) -> list[tuple[int, int]]:
    """Re-fetch entries and return ``[(rank, entry_id), ...]`` in rank order."""
    rows = await db.execute(
        select(BoardEntry.position, BoardEntry.id)  # type: ignore[call-overload]
        .where(BoardEntry.board_id == board_id)  # type: ignore[arg-type]
        .order_by(BoardEntry.position)  # type: ignore[arg-type]
    )
    return [(r.position, r.id) for r in rows.all()]


@pytest.mark.asyncio
async def test_move_entry_swaps_neighbor_ranks(
    db_session: AsyncSession,
    news_source: NewsSource,
    players: list[PlayerMaster],
) -> None:
    """move_entry up/down swaps ranks with neighbor; boundary is a no-op."""
    assert news_source.id is not None
    board = await svc.create_board(
        db_session,
        news_source_id=news_source.id,
        draft_year=2026,
        published_at=_published_at(),
        entries=[
            svc.EntryInput(player_id=players[0].id, position=1),  # type: ignore[arg-type]
            svc.EntryInput(player_id=players[1].id, position=2),  # type: ignore[arg-type]
            svc.EntryInput(player_id=players[2].id, position=3),  # type: ignore[arg-type]
        ],
    )
    await db_session.commit()
    board_id = board.id
    assert board_id is not None

    initial = await _ranks_by_entry_id(db_session, board_id)
    rank1_id, rank2_id, rank3_id = (entry_id for _, entry_id in initial)

    await svc.move_entry(db_session, entry_id=rank2_id, direction="up")
    await db_session.commit()
    assert (await _ranks_by_entry_id(db_session, board_id)) == [
        (1, rank2_id),
        (2, rank1_id),
        (3, rank3_id),
    ]

    # Already at the top -> no-op
    await svc.move_entry(db_session, entry_id=rank2_id, direction="up")
    await db_session.commit()
    assert (await _ranks_by_entry_id(db_session, board_id)) == [
        (1, rank2_id),
        (2, rank1_id),
        (3, rank3_id),
    ]

    # Bottom entry down -> no-op
    await svc.move_entry(db_session, entry_id=rank3_id, direction="down")
    await db_session.commit()
    final = await _ranks_by_entry_id(db_session, board_id)
    assert final[-1] == (3, rank3_id)


@pytest.mark.asyncio
async def test_move_entry_blocked_on_approved_board(
    db_session: AsyncSession,
    news_source: NewsSource,
    players: list[PlayerMaster],
) -> None:
    """Approved boards refuse move operations like all other mutations."""
    assert news_source.id is not None
    board = await svc.create_board(
        db_session,
        news_source_id=news_source.id,
        draft_year=2026,
        published_at=_published_at(),
        entries=[
            svc.EntryInput(player_id=players[0].id, position=1),  # type: ignore[arg-type]
            svc.EntryInput(player_id=players[1].id, position=2),  # type: ignore[arg-type]
        ],
    )
    await svc.approve_board(db_session, board_id=board.id)  # type: ignore[arg-type]
    await db_session.commit()

    _, entries = await svc.get_board_with_entries(db_session, board.id)  # type: ignore[arg-type]
    with pytest.raises(svc.BoardNotEditableError):
        await svc.move_entry(db_session, entry_id=entries[0].id, direction="down")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_latest_entry_tier_returns_tier_of_highest_rank(
    db_session: AsyncSession,
    news_source: NewsSource,
    players: list[PlayerMaster],
) -> None:
    """latest_entry_tier looks at the entry with the largest rank value."""
    assert news_source.id is not None
    board = await svc.create_board(
        db_session,
        news_source_id=news_source.id,
        draft_year=2026,
        published_at=_published_at(),
        entries=[
            svc.EntryInput(player_id=players[0].id, position=1, tier=1),  # type: ignore[arg-type]
            svc.EntryInput(player_id=players[1].id, position=2, tier=2),  # type: ignore[arg-type]
        ],
    )
    await db_session.commit()
    assert (
        await svc.latest_entry_tier(db_session, board_id=board.id)  # type: ignore[arg-type]
    ) == 2

    empty = await svc.create_board(
        db_session,
        news_source_id=news_source.id,
        draft_year=2026,
        published_at=_published_at() - timedelta(days=1),
        entries=[],
    )
    await db_session.commit()
    assert (
        await svc.latest_entry_tier(db_session, board_id=empty.id)  # type: ignore[arg-type]
    ) is None


@pytest.mark.asyncio
async def test_list_boards_filters_by_status_source_and_year(
    db_session: AsyncSession,
    news_source: NewsSource,
    players: list[PlayerMaster],
) -> None:
    """list_boards filters compose; results sorted by published_at desc."""
    assert news_source.id is not None
    other_source = NewsSource(
        name="Other Source",
        display_name="Other Source",
        feed_url="https://example.com/other-feed",
    )
    db_session.add(other_source)
    await db_session.flush()
    assert other_source.id is not None

    # Two PENDING from news_source, one APPROVED from news_source (different
    # year), one PENDING from other_source.
    pending_now = await svc.create_board(
        db_session,
        news_source_id=news_source.id,
        draft_year=2026,
        published_at=datetime.utcnow(),
        entries=[svc.EntryInput(player_id=players[0].id, position=1)],  # type: ignore[arg-type]
    )
    pending_older = await svc.create_board(
        db_session,
        news_source_id=news_source.id,
        draft_year=2026,
        published_at=datetime.utcnow() - timedelta(days=2),
        entries=[svc.EntryInput(player_id=players[1].id, position=1)],  # type: ignore[arg-type]
    )
    approved_other_year = await svc.create_board(
        db_session,
        news_source_id=news_source.id,
        draft_year=2025,
        published_at=datetime.utcnow() - timedelta(days=10),
        entries=[svc.EntryInput(player_id=players[2].id, position=1)],  # type: ignore[arg-type]
    )
    await svc.approve_board(db_session, board_id=approved_other_year.id)  # type: ignore[arg-type]
    other_source_board = await svc.create_board(
        db_session,
        news_source_id=other_source.id,
        draft_year=2026,
        published_at=datetime.utcnow() - timedelta(days=1),
        entries=[],
    )
    await db_session.commit()

    pending_2026 = await svc.list_boards(
        db_session, status=BoardStatus.PENDING, draft_year=2026
    )
    assert [b.id for b in pending_2026] == [
        pending_now.id,
        other_source_board.id,
        pending_older.id,
    ]

    only_news_source = await svc.list_boards(
        db_session, news_source_id=news_source.id
    )
    assert {b.id for b in only_news_source} == {
        pending_now.id,
        pending_older.id,
        approved_other_year.id,
    }


@pytest.mark.asyncio
async def test_board_entry_unresolved_round_trip(
    db_session: AsyncSession,
    news_source: NewsSource,
) -> None:
    """BoardEntry with player_id=None and UNRESOLVED resolution_method persists and round-trips.

    Verifies the entity-resolution schema columns: raw_name (NOT NULL),
    resolution_method (enum), player_id (NULLABLE), and vector_candidates
    (JSONB) can all be written and read back without error.
    """
    assert news_source.id is not None

    board = Board(
        news_source_id=news_source.id,
        draft_year=2026,
        published_at=_published_at(),
        size=1,
        status=BoardStatus.PENDING,
    )
    db_session.add(board)
    await db_session.flush()
    assert board.id is not None

    candidates = [
        {"player_id": 99, "display_name": "Mara Jones", "score": 0.91},
        {"player_id": 100, "display_name": "Mara Smith", "score": 0.87},
    ]
    entry = BoardEntry(
        board_id=board.id,
        player_id=None,
        position=1,
        raw_name="Mara",
        resolution_method=ResolutionMethod.UNRESOLVED,
        vector_candidates=candidates,
    )
    db_session.add(entry)
    await db_session.flush()
    assert entry.id is not None

    await db_session.commit()

    fetched = await db_session.get(BoardEntry, entry.id)
    assert fetched is not None
    assert fetched.player_id is None
    assert fetched.raw_name == "Mara"
    assert fetched.resolution_method is ResolutionMethod.UNRESOLVED
    assert fetched.vector_candidates == candidates


@pytest.mark.asyncio
async def test_board_entry_resolved_stores_raw_name_and_method(
    db_session: AsyncSession,
    news_source: NewsSource,
    players: list[PlayerMaster],
) -> None:
    """A resolved BoardEntry stores raw_name and resolution_method alongside player_id.

    Checks that the entity-resolution columns coexist correctly with a
    non-NULL player_id on a standard resolved entry.
    """
    assert news_source.id is not None
    board = await svc.create_board(
        db_session,
        news_source_id=news_source.id,
        draft_year=2026,
        published_at=_published_at(),
        entries=[
            svc.EntryInput(
                player_id=players[0].id,  # type: ignore[arg-type]
                position=1,
                raw_name="Cooper Flagg",
                resolution_method=ResolutionMethod.EXACT,
            ),
        ],
    )
    await db_session.commit()

    _, entries = await svc.get_board_with_entries(db_session, board.id)  # type: ignore[arg-type]
    assert len(entries) == 1
    e = entries[0]
    assert e.player_id == players[0].id
    assert e.raw_name == "Cooper Flagg"
    assert e.resolution_method is ResolutionMethod.EXACT
    assert e.vector_candidates is None
