"""Integration tests for board_extraction_service.extract_board.

Patches the network (fetcher) and Gemini calls; everything else hits the
real DB through the fixtures in ``tests/integration/conftest.py``.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.schemas.boards import Board, BoardEntry, BoardKind, BoardStatus
from app.schemas.news_items import NewsItem, NewsItemTag
from app.schemas.news_sources import NewsSource
from app.schemas.players_master import PlayerMaster
from app.services import board_extraction_service
from app.services.board_extraction_service import (
    ExtractedBoard,
    ExtractedBoardEntry,
    extract_board,
)


# --- Helpers ----------------------------------------------------------------


async def _make_news_item(
    db: AsyncSession,
    *,
    source_id: int,
    url: str = "https://example.substack.com/p/2026-big-board",
    external_id: str = "test-board-1",
) -> NewsItem:
    item = NewsItem(
        source_id=source_id,
        external_id=external_id,
        title="2026 Big Board",
        description="The latest update.",
        url=url,
        tag=NewsItemTag.BIG_BOARD,
        published_at=datetime(2026, 3, 15),
    )
    db.add(item)
    await db.flush()
    return item


async def _make_player(db: AsyncSession, *, display_name: str) -> PlayerMaster:
    player = PlayerMaster(display_name=display_name)
    db.add(player)
    await db.flush()
    return player


def _fake_fetcher(text: str):
    async def _f(url: str) -> str:
        return text

    return _f


def _stub_extraction(monkeypatch, extracted: ExtractedBoard) -> None:
    """Replace _extract_via_gemini with a fixed-output stub."""

    async def _stub(article_text: str, *, client=None) -> ExtractedBoard:
        assert article_text, "Service should have fetched non-empty article text"
        return extracted

    monkeypatch.setattr(board_extraction_service, "_extract_via_gemini", _stub)


# --- Happy path ------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_board_creates_pending_with_resolved_players(
    db_session: AsyncSession,
    news_source: NewsSource,
    monkeypatch,
) -> None:
    """A well-formed extraction creates a PENDING Board with matched players."""
    assert news_source.id is not None
    item = await _make_news_item(db_session, source_id=news_source.id)
    flagg = await _make_player(db_session, display_name="Cooper Flagg")
    harper = await _make_player(db_session, display_name="Dylan Harper")

    _stub_extraction(
        monkeypatch,
        ExtractedBoard(
            draft_year=2026,
            published_at=datetime(2026, 3, 15),
            entries=[
                ExtractedBoardEntry(player_name="Cooper Flagg", rank=1, tier=1),
                ExtractedBoardEntry(player_name="Dylan Harper", rank=2, tier=1),
            ],
        ),
    )

    assert item.id is not None
    board = await extract_board(
        db_session,
        news_item_id=item.id,
        fetcher=_fake_fetcher("Plenty of board content here."),
    )

    assert board is not None
    assert board.status == BoardStatus.PENDING
    assert board.kind == BoardKind.BIG_BOARD
    assert board.draft_year == 2026
    assert board.size == 2

    entries_stmt = select(BoardEntry).where(BoardEntry.board_id == board.id)
    result = await db_session.execute(entries_stmt)
    entries = sorted(result.scalars().all(), key=lambda e: e.position)

    assert [e.player_id for e in entries] == [flagg.id, harper.id]
    assert [e.position for e in entries] == [1, 2]
    assert all(e.tier == 1 for e in entries)


# --- Unmatched player names ------------------------------------------------


@pytest.mark.asyncio
async def test_extract_board_drops_unmatched_players(
    db_session: AsyncSession,
    news_source: NewsSource,
    monkeypatch,
) -> None:
    """Unknown player names are dropped silently; matched ones still land."""
    assert news_source.id is not None
    item = await _make_news_item(db_session, source_id=news_source.id)
    flagg = await _make_player(db_session, display_name="Cooper Flagg")

    _stub_extraction(
        monkeypatch,
        ExtractedBoard(
            draft_year=2026,
            entries=[
                ExtractedBoardEntry(player_name="Cooper Flagg", rank=1),
                ExtractedBoardEntry(player_name="Nobody Player", rank=2),
            ],
        ),
    )

    assert item.id is not None
    board = await extract_board(
        db_session,
        news_item_id=item.id,
        fetcher=_fake_fetcher("article text"),
    )

    assert board is not None
    entries_stmt = select(BoardEntry).where(BoardEntry.board_id == board.id)
    result = await db_session.execute(entries_stmt)
    entries = result.scalars().all()

    assert len(entries) == 1
    assert entries[0].player_id == flagg.id


# --- No resolvable entries → returns None, no Board created ---------------


@pytest.mark.asyncio
async def test_extract_board_returns_none_when_no_players_match(
    db_session: AsyncSession,
    news_source: NewsSource,
    monkeypatch,
) -> None:
    assert news_source.id is not None
    item = await _make_news_item(db_session, source_id=news_source.id)

    _stub_extraction(
        monkeypatch,
        ExtractedBoard(
            draft_year=2026,
            entries=[
                ExtractedBoardEntry(player_name="Nobody One", rank=1),
                ExtractedBoardEntry(player_name="Nobody Two", rank=2),
            ],
        ),
    )

    assert item.id is not None
    board = await extract_board(
        db_session,
        news_item_id=item.id,
        fetcher=_fake_fetcher("article text"),
    )

    assert board is None
    # And no Board row was created.
    result = await db_session.execute(
        select(Board).where(Board.news_item_id == item.id)
    )
    assert result.scalar_one_or_none() is None


# --- Dedup behavior --------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_board_returns_existing_pending_unchanged(
    db_session: AsyncSession,
    news_source: NewsSource,
    monkeypatch,
) -> None:
    """A second call against the same NewsItem returns the existing PENDING board.

    Verifies dedup: the second call does not write a new board.
    """
    assert news_source.id is not None
    item = await _make_news_item(db_session, source_id=news_source.id)
    await _make_player(db_session, display_name="Cooper Flagg")

    extracted = ExtractedBoard(
        draft_year=2026,
        entries=[ExtractedBoardEntry(player_name="Cooper Flagg", rank=1)],
    )
    _stub_extraction(monkeypatch, extracted)

    assert item.id is not None
    first = await extract_board(
        db_session,
        news_item_id=item.id,
        fetcher=_fake_fetcher("article text"),
    )
    assert first is not None

    second = await extract_board(
        db_session,
        news_item_id=item.id,
        fetcher=_fake_fetcher("article text"),
    )
    assert second is not None
    assert second.id == first.id

    result = await db_session.execute(
        select(Board).where(Board.news_item_id == item.id)
    )
    boards = result.scalars().all()
    assert len(boards) == 1


# --- Entity resolution: diacritic-insensitive matching --------------------


@pytest.mark.asyncio
async def test_extract_board_matches_across_diacritics(
    db_session: AsyncSession,
    news_source: NewsSource,
    monkeypatch,
) -> None:
    """A DB row with diacritics matches an extracted ASCII name (and vice versa)."""
    assert news_source.id is not None
    item = await _make_news_item(db_session, source_id=news_source.id)
    maledon = await _make_player(db_session, display_name="Théo Maledon")

    _stub_extraction(
        monkeypatch,
        ExtractedBoard(
            draft_year=2026,
            entries=[ExtractedBoardEntry(player_name="Theo Maledon", rank=1)],
        ),
    )

    assert item.id is not None
    board = await extract_board(
        db_session,
        news_item_id=item.id,
        fetcher=_fake_fetcher("article text"),
    )

    assert board is not None
    entries_stmt = select(BoardEntry).where(BoardEntry.board_id == board.id)
    result = await db_session.execute(entries_stmt)
    entries = result.scalars().all()
    assert len(entries) == 1
    assert entries[0].player_id == maledon.id


@pytest.mark.asyncio
async def test_extract_board_matches_when_db_name_starts_with_diacritic(
    db_session: AsyncSession,
    news_source: NewsSource,
    monkeypatch,
) -> None:
    """A DB row starting with a diacritic character is found even though the
    extracted (normalized) needle starts with a plain ASCII letter.

    Previously a SQL prefix prefilter (LIKE 'e%') would skip "Éric ..." rows
    because lower('É') != 'e'. The fix loads candidates without a prefix
    filter and matches Python-side on the normalized form.
    """
    assert news_source.id is not None
    item = await _make_news_item(db_session, source_id=news_source.id)
    eric = await _make_player(db_session, display_name="Éric Dubois")

    _stub_extraction(
        monkeypatch,
        ExtractedBoard(
            draft_year=2026,
            entries=[ExtractedBoardEntry(player_name="Eric Dubois", rank=1)],
        ),
    )

    assert item.id is not None
    board = await extract_board(
        db_session,
        news_item_id=item.id,
        fetcher=_fake_fetcher("article text"),
    )

    assert board is not None
    entries_stmt = select(BoardEntry).where(BoardEntry.board_id == board.id)
    result = await db_session.execute(entries_stmt)
    entries = result.scalars().all()
    assert len(entries) == 1
    assert entries[0].player_id == eric.id


@pytest.mark.asyncio
async def test_extract_board_matches_across_suffix(
    db_session: AsyncSession,
    news_source: NewsSource,
    monkeypatch,
) -> None:
    """A "Bronny James" in the DB matches an extracted "Bronny James Jr."."""
    assert news_source.id is not None
    item = await _make_news_item(db_session, source_id=news_source.id)
    bronny = await _make_player(db_session, display_name="Bronny James")

    _stub_extraction(
        monkeypatch,
        ExtractedBoard(
            draft_year=2026,
            entries=[ExtractedBoardEntry(player_name="Bronny James Jr.", rank=1)],
        ),
    )

    assert item.id is not None
    board = await extract_board(
        db_session,
        news_item_id=item.id,
        fetcher=_fake_fetcher("article text"),
    )

    assert board is not None
    entries_stmt = select(BoardEntry).where(BoardEntry.board_id == board.id)
    result = await db_session.execute(entries_stmt)
    entries = result.scalars().all()
    assert len(entries) == 1
    assert entries[0].player_id == bronny.id


@pytest.mark.asyncio
async def test_extract_board_treats_ambiguous_name_as_unresolved(
    db_session: AsyncSession,
    news_source: NewsSource,
    monkeypatch,
) -> None:
    """Two players sharing a display name → ambiguous match, treated as unresolved.

    The function does not crash or pick arbitrarily; the other (legitimate)
    entry still lands.
    """
    assert news_source.id is not None
    item = await _make_news_item(db_session, source_id=news_source.id)
    await _make_player(db_session, display_name="Justin Edwards")
    await _make_player(db_session, display_name="Justin Edwards")  # collision
    flagg = await _make_player(db_session, display_name="Cooper Flagg")

    _stub_extraction(
        monkeypatch,
        ExtractedBoard(
            draft_year=2026,
            entries=[
                ExtractedBoardEntry(player_name="Cooper Flagg", rank=1),
                ExtractedBoardEntry(player_name="Justin Edwards", rank=2),
            ],
        ),
    )

    assert item.id is not None
    board = await extract_board(
        db_session,
        news_item_id=item.id,
        fetcher=_fake_fetcher("article text"),
    )

    # The unambiguous entry lands; the ambiguous one is dropped (until
    # #225 enables persisting it as an unresolved entry).
    assert board is not None
    entries_stmt = select(BoardEntry).where(BoardEntry.board_id == board.id)
    result = await db_session.execute(entries_stmt)
    entries = result.scalars().all()
    assert len(entries) == 1
    assert entries[0].player_id == flagg.id


# --- MOCK_DRAFT extraction is intentionally not supported in this iteration


@pytest.mark.asyncio
async def test_extract_board_rejects_mock_draft_kind(
    db_session: AsyncSession,
    news_source: NewsSource,
) -> None:
    assert news_source.id is not None
    item = await _make_news_item(db_session, source_id=news_source.id)
    assert item.id is not None

    with pytest.raises(NotImplementedError):
        await extract_board(
            db_session,
            news_item_id=item.id,
            kind=BoardKind.MOCK_DRAFT,
        )
