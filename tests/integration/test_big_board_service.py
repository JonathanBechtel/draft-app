"""Integration tests for the big board service layer.

These exercise the service against a live test DB session so the unique
constraints and status guards behave the way the routes will rely on.
"""

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.big_boards import BigBoard, BigBoardEntry, BoardStatus
from app.schemas.news_sources import NewsSource
from app.schemas.players_master import PlayerMaster
from app.services import big_board_service as svc
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
    """create_board writes PENDING board + ordered entries; board_size matches."""
    assert news_source.id is not None
    board = await svc.create_board(
        db_session,
        news_source_id=news_source.id,
        draft_year=2026,
        published_at=_published_at(),
        entries=[
            svc.EntryInput(player_id=players[0].id, rank=1, tier=1),  # type: ignore[arg-type]
            svc.EntryInput(player_id=players[1].id, rank=2, tier=1),  # type: ignore[arg-type]
            svc.EntryInput(player_id=players[2].id, rank=3, tier=2),  # type: ignore[arg-type]
        ],
    )
    await db_session.commit()

    assert board.status is BoardStatus.PENDING
    assert board.approved_at is None
    assert board.board_size == 3

    _, entries = await svc.get_board_with_entries(db_session, board.id)  # type: ignore[arg-type]
    assert [(e.rank, e.player_id, e.tier) for e in entries] == [
        (1, players[0].id, 1),
        (2, players[1].id, 1),
        (3, players[2].id, 2),
    ]


@pytest.mark.asyncio
async def test_create_board_rejects_duplicate_rank(
    db_session: AsyncSession,
    news_source: NewsSource,
    players: list[PlayerMaster],
) -> None:
    """Two entries at rank 1 on the same board raises DuplicateRankError."""
    assert news_source.id is not None
    with pytest.raises(svc.DuplicateRankError):
        await svc.create_board(
            db_session,
            news_source_id=news_source.id,
            draft_year=2026,
            published_at=_published_at(),
            entries=[
                svc.EntryInput(player_id=players[0].id, rank=1),  # type: ignore[arg-type]
                svc.EntryInput(player_id=players[1].id, rank=1),  # type: ignore[arg-type]
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
                svc.EntryInput(player_id=players[0].id, rank=1),  # type: ignore[arg-type]
                svc.EntryInput(player_id=players[0].id, rank=2),  # type: ignore[arg-type]
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
        entries=[svc.EntryInput(player_id=players[0].id, rank=1)],  # type: ignore[arg-type]
    )
    await db_session.commit()

    added = await svc.add_entry(
        db_session,
        board_id=board.id,  # type: ignore[arg-type]
        player_id=players[1].id,  # type: ignore[arg-type]
        rank=2,
    )
    await db_session.commit()
    assert board.board_size == 2

    await svc.update_entry(
        db_session, entry_id=added.id, rank=5, tier=3  # type: ignore[arg-type]
    )
    await db_session.commit()
    refreshed = await db_session.get(BigBoardEntry, added.id)
    assert refreshed is not None
    assert refreshed.rank == 5
    assert refreshed.tier == 3

    await svc.delete_entry(db_session, entry_id=added.id)  # type: ignore[arg-type]
    await db_session.commit()
    assert await db_session.get(BigBoardEntry, added.id) is None
    refreshed_board = await db_session.get(BigBoard, board.id)
    assert refreshed_board is not None
    assert refreshed_board.board_size == 1


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
        entries=[svc.EntryInput(player_id=players[0].id, rank=1)],  # type: ignore[arg-type]
    )
    await db_session.commit()

    approved = await svc.approve_board(db_session, board_id=board.id)  # type: ignore[arg-type]
    await db_session.commit()
    assert approved.status is BoardStatus.APPROVED
    assert approved.approved_at is not None

    entry_id = (
        await db_session.execute(
            select(BigBoardEntry.id).where(  # type: ignore[call-overload]
                BigBoardEntry.board_id == board.id  # type: ignore[arg-type]
            )
        )
    ).scalar_one()

    with pytest.raises(svc.BoardNotEditableError):
        await svc.add_entry(
            db_session,
            board_id=board.id,  # type: ignore[arg-type]
            player_id=players[1].id,  # type: ignore[arg-type]
            rank=2,
        )
    with pytest.raises(svc.BoardNotEditableError):
        await svc.update_entry(db_session, entry_id=entry_id, rank=99)
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
        entries=[svc.EntryInput(player_id=players[0].id, rank=1)],  # type: ignore[arg-type]
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
            svc.EntryInput(player_id=players[0].id, rank=1),  # type: ignore[arg-type]
            svc.EntryInput(player_id=players[1].id, rank=2),  # type: ignore[arg-type]
        ],
    )
    await db_session.commit()
    board_id = board.id

    await svc.delete_board(db_session, board_id=board_id)  # type: ignore[arg-type]
    await db_session.commit()

    assert await db_session.get(BigBoard, board_id) is None
    remaining = (
        await db_session.execute(
            select(BigBoardEntry).where(  # type: ignore[call-overload]
                BigBoardEntry.board_id == board_id  # type: ignore[arg-type]
            )
        )
    ).scalars().all()
    assert remaining == []


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
        entries=[svc.EntryInput(player_id=players[0].id, rank=1)],  # type: ignore[arg-type]
    )
    pending_older = await svc.create_board(
        db_session,
        news_source_id=news_source.id,
        draft_year=2026,
        published_at=datetime.utcnow() - timedelta(days=2),
        entries=[svc.EntryInput(player_id=players[1].id, rank=1)],  # type: ignore[arg-type]
    )
    approved_other_year = await svc.create_board(
        db_session,
        news_source_id=news_source.id,
        draft_year=2025,
        published_at=datetime.utcnow() - timedelta(days=10),
        entries=[svc.EntryInput(player_id=players[2].id, rank=1)],  # type: ignore[arg-type]
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
