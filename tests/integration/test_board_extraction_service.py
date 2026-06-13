"""Integration tests for board_extraction_service.extract_board.

Patches the network (fetcher) and Gemini calls; everything else hits the
real DB through the fixtures in ``tests/integration/conftest.py``.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.schemas.boards import (
    Board,
    BoardEntry,
    BoardKind,
    BoardStatus,
    ResolutionMethod,
)
from app.schemas.news_items import NewsItem, NewsItemTag
from app.schemas.news_sources import NewsSource
from app.schemas.player_aliases import PlayerAlias
from app.schemas.players_master import PlayerMaster
from app.services import board_extraction_service
from app.services.board_extraction_service import (
    ExtractedBoard,
    ExtractedBoardEntry,
    PaywallDetectedError,
    extract_board,
)
from app.services.player_search_service import Candidate


_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "substack"


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


def _stub_vector_search(
    monkeypatch,
    candidates: list[Candidate] | None = None,
) -> None:
    """Patch find_candidate_players in board_extraction_service to return canned results.

    Prevents any live Gemini embedding or DB trigram/vector-search calls in
    tests.  Pass ``candidates=None`` (default) to simulate an empty result set.

    Note: ``resolve_player`` uses ``find_candidate_players`` (the hybrid
    lexical+vector function) rather than the bare ``find_similar_players``
    since T10.  The patch target is updated accordingly.
    """
    _candidates = candidates or []

    async def _stub(db, query: str, k: int = 5) -> list[Candidate]:
        return _candidates

    monkeypatch.setattr(board_extraction_service, "find_candidate_players", _stub)


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


# --- Mock-draft ingestion (unified ranking model) -------------------------


@pytest.mark.asyncio
async def test_extract_board_mock_draft_persists_ranking(
    db_session: AsyncSession,
    news_source: NewsSource,
    monkeypatch,
) -> None:
    """A MOCK_DRAFT article is ingested as a ranking (position = pick number).

    Confirms the unified-ranking model: the same extraction path runs for
    kind=MOCK_DRAFT, persists a PENDING board with derived ``num_rounds``,
    resolves players, and leaves per-pick team fields unset (pick ownership
    is sourced from the draft-order reference, not the article).
    """
    assert news_source.id is not None
    item = await _make_news_item(
        db_session,
        source_id=news_source.id,
        url="https://example.substack.com/p/2026-mock-draft",
        external_id="test-mock-1",
    )
    item.tag = NewsItemTag.MOCK_DRAFT
    await db_session.flush()

    flagg = await _make_player(db_session, display_name="Cooper Flagg")
    harper = await _make_player(db_session, display_name="Dylan Harper")

    # Extraction returns the same shape as a big board: an ordered player
    # list. For a mock the "rank" is the pick number; no team is captured.
    _stub_extraction(
        monkeypatch,
        ExtractedBoard(
            draft_year=2026,
            published_at=datetime(2026, 6, 1),
            entries=[
                ExtractedBoardEntry(player_name="Cooper Flagg", rank=1),
                ExtractedBoardEntry(player_name="Dylan Harper", rank=2),
            ],
        ),
    )

    assert item.id is not None
    board = await extract_board(
        db_session,
        news_item_id=item.id,
        kind=BoardKind.MOCK_DRAFT,
        fetcher=_fake_fetcher("1. ATL — Cooper Flagg. 2. WAS — Dylan Harper."),
    )

    assert board is not None
    assert board.status == BoardStatus.PENDING
    assert board.kind == BoardKind.MOCK_DRAFT
    # Two picks, both inside round one → single-round mock.
    assert board.num_rounds == 1
    assert board.size == 2

    entries_stmt = select(BoardEntry).where(BoardEntry.board_id == board.id)
    result = await db_session.execute(entries_stmt)
    entries = sorted(result.scalars().all(), key=lambda e: e.position)

    assert [e.player_id for e in entries] == [flagg.id, harper.id]
    assert [e.position for e in entries] == [1, 2]
    # Pick ownership is NOT inferred from the article under the unified model.
    assert all(e.team_id is None for e in entries)
    assert all(e.round is None for e in entries)
    assert all(e.trade_note is None for e in entries)


# --- Resolution cascade: ALIAS branch -------------------------------------


@pytest.mark.asyncio
async def test_extract_board_resolves_via_alias(
    db_session: AsyncSession,
    news_source: NewsSource,
    monkeypatch,
) -> None:
    """A name that misses display_name but matches a player_alias resolves as ALIAS.

    Confirms the second step of the cascade is reachable and records the
    correct ResolutionMethod on the persisted entry.
    """
    assert news_source.id is not None
    item = await _make_news_item(db_session, source_id=news_source.id)

    # Create a player whose display_name won't match the extracted name.
    player = await _make_player(db_session, display_name="Lonzo Ball")
    assert player.id is not None

    # Create an alias that will match the extraction output.
    alias = PlayerAlias(player_id=player.id, full_name="Zo Ball")
    db_session.add(alias)
    await db_session.flush()

    _stub_extraction(
        monkeypatch,
        ExtractedBoard(
            draft_year=2026,
            entries=[
                ExtractedBoardEntry(player_name="Zo Ball", rank=1),
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
    assert board.size == 1

    entries_stmt = (
        select(BoardEntry).where(BoardEntry.board_id == board.id)  # type: ignore[arg-type]
    )
    result = await db_session.execute(entries_stmt)
    entries = list(result.scalars().all())

    assert len(entries) == 1
    assert entries[0].player_id == player.id
    assert entries[0].resolution_method == ResolutionMethod.ALIAS
    assert entries[0].raw_name == "Zo Ball"
    assert entries[0].vector_candidates is None


# --- Unmatched player names ------------------------------------------------


@pytest.mark.asyncio
async def test_extract_board_persists_unresolved_players(
    db_session: AsyncSession,
    news_source: NewsSource,
    monkeypatch,
) -> None:
    """Unknown player names are persisted as UNRESOLVED entries, not dropped.

    The board contains both the matched and unmatched entries. The unmatched
    entry carries player_id=None, method=UNRESOLVED, and the top-K vector
    candidates for admin review.
    """
    assert news_source.id is not None
    item = await _make_news_item(db_session, source_id=news_source.id)
    flagg = await _make_player(db_session, display_name="Cooper Flagg")

    # Two fake vector candidates returned by the mocked search.
    fake_candidates = [
        Candidate(
            player_id=999, display_name="Nobody Player", school="Duke", score=0.85
        ),
        Candidate(player_id=998, display_name="Somebody Else", school=None, score=0.70),
    ]
    _stub_vector_search(monkeypatch, candidates=fake_candidates)

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
    assert board.size == 2  # Both entries landed.

    entries_stmt = (
        select(BoardEntry)
        .where(BoardEntry.board_id == board.id)  # type: ignore[arg-type]
        .order_by(BoardEntry.position)  # type: ignore[attr-defined]
    )
    result = await db_session.execute(entries_stmt)
    entries = list(result.scalars().all())

    assert len(entries) == 2

    # First entry: resolved by EXACT match.
    assert entries[0].player_id == flagg.id
    assert entries[0].resolution_method == ResolutionMethod.EXACT
    assert entries[0].vector_candidates is None

    # Second entry: unresolved, carries vector candidates.
    assert entries[1].player_id is None
    assert entries[1].resolution_method == ResolutionMethod.UNRESOLVED
    assert entries[1].raw_name == "Nobody Player"
    assert entries[1].vector_candidates is not None
    assert len(entries[1].vector_candidates) == 2
    assert entries[1].vector_candidates[0]["player_id"] == 999
    assert entries[1].vector_candidates[0]["display_name"] == "Nobody Player"
    assert entries[1].vector_candidates[1]["player_id"] == 998


# --- All entries unresolved → board still created with UNRESOLVED entries ---


@pytest.mark.asyncio
async def test_extract_board_creates_board_when_all_players_unresolved(
    db_session: AsyncSession,
    news_source: NewsSource,
    monkeypatch,
) -> None:
    """When no player name resolves, a PENDING board is still created.

    Every entry persists as UNRESOLVED so an admin can review and resolve
    them. The board is NOT None — silently dropping all work is the old
    behaviour; the new behaviour is preserve-everything-for-review.
    """
    assert news_source.id is not None
    item = await _make_news_item(db_session, source_id=news_source.id)

    _stub_vector_search(monkeypatch, candidates=[])  # No helpful candidates.

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

    # Board is created — it contains unresolved entries awaiting admin review.
    assert board is not None
    assert board.status == BoardStatus.PENDING
    assert board.size == 2

    entries_stmt = (
        select(BoardEntry)
        .where(BoardEntry.board_id == board.id)  # type: ignore[arg-type]
        .order_by(BoardEntry.position)  # type: ignore[attr-defined]
    )
    result = await db_session.execute(entries_stmt)
    entries = list(result.scalars().all())

    assert len(entries) == 2
    assert all(e.player_id is None for e in entries)
    assert all(e.resolution_method == ResolutionMethod.UNRESOLVED for e in entries)
    assert entries[0].raw_name == "Nobody One"
    assert entries[1].raw_name == "Nobody Two"


@pytest.mark.asyncio
async def test_extract_board_returns_none_only_when_gemini_emits_no_entries(
    db_session: AsyncSession,
    news_source: NewsSource,
    monkeypatch,
) -> None:
    """extract_board returns None only when Gemini extracts zero entries.

    This is the only remaining case where no board is created; it is
    unrelated to player resolution (there are simply no ranked entries to
    persist at all).
    """
    assert news_source.id is not None
    item = await _make_news_item(db_session, source_id=news_source.id)

    _stub_extraction(
        monkeypatch,
        ExtractedBoard(draft_year=2026, entries=[]),
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
        select(Board).where(Board.news_item_id == item.id)  # type: ignore[arg-type]
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
    """Two players sharing a display name → ambiguous match, persisted as UNRESOLVED.

    The function does not crash or pick arbitrarily. The unambiguous entry
    resolves normally; the ambiguous one lands as UNRESOLVED with vector
    candidates so an admin can pick the correct player.
    """
    assert news_source.id is not None
    item = await _make_news_item(db_session, source_id=news_source.id)
    await _make_player(db_session, display_name="Justin Edwards")
    await _make_player(db_session, display_name="Justin Edwards")  # collision
    flagg = await _make_player(db_session, display_name="Cooper Flagg")

    _stub_vector_search(monkeypatch, candidates=[])  # No helpful candidates.

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

    # Both entries persist: the unambiguous one is resolved, the ambiguous
    # one is UNRESOLVED with player_id=None for admin review.
    assert board is not None
    assert board.size == 2

    entries_stmt = (
        select(BoardEntry)
        .where(BoardEntry.board_id == board.id)  # type: ignore[arg-type]
        .order_by(BoardEntry.position)  # type: ignore[attr-defined]
    )
    result = await db_session.execute(entries_stmt)
    entries = list(result.scalars().all())

    assert len(entries) == 2
    assert entries[0].player_id == flagg.id
    assert entries[0].resolution_method == ResolutionMethod.EXACT

    assert entries[1].player_id is None
    assert entries[1].resolution_method == ResolutionMethod.UNRESOLVED
    assert entries[1].raw_name == "Justin Edwards"


# --- Substack API routing through _default_fetcher ------------------------


@pytest.mark.asyncio
async def test_extract_board_routes_substack_url_through_api(
    db_session: AsyncSession,
    news_source: NewsSource,
    monkeypatch,
) -> None:
    """A Substack post URL hits the JSON API path and lands a PENDING Board.

    The HTTP layer is stubbed to return the captured fixture payload,
    so ``_default_fetcher`` walks the full Substack branch (URL
    translation → API fetch → ``body_html`` text extraction) before
    handing off to a stubbed Gemini.
    """
    assert news_source.id is not None
    item = await _make_news_item(
        db_session,
        source_id=news_source.id,
        url="https://example.substack.com/p/2026-nba-draft-big-board-v3",
    )
    flagg = await _make_player(db_session, display_name="Cooper Flagg")
    harper = await _make_player(db_session, display_name="Dylan Harper")

    fixture_payload = json.loads((_FIXTURE_DIR / "big_board_free.json").read_text())
    api_urls_called: list[str] = []

    async def _stub_http_get(url: str) -> str:
        api_urls_called.append(url)
        return json.dumps(fixture_payload)

    monkeypatch.setattr(board_extraction_service, "_http_get", _stub_http_get)

    _stub_extraction(
        monkeypatch,
        ExtractedBoard(
            draft_year=2026,
            published_at=datetime(2026, 5, 15),
            entries=[
                ExtractedBoardEntry(player_name="Cooper Flagg", rank=1, tier=1),
                ExtractedBoardEntry(player_name="Dylan Harper", rank=2, tier=1),
            ],
        ),
    )

    assert item.id is not None
    board = await extract_board(db_session, news_item_id=item.id)

    assert board is not None
    assert board.status == BoardStatus.PENDING
    assert board.size == 2

    # Confirms the fetcher translated the post URL into the API URL.
    assert api_urls_called == [
        "https://example.substack.com/api/v1/posts/2026-nba-draft-big-board-v3"
    ]

    entries_stmt = select(BoardEntry).where(BoardEntry.board_id == board.id)
    result = await db_session.execute(entries_stmt)
    entries = sorted(result.scalars().all(), key=lambda e: e.position)
    assert [e.player_id for e in entries] == [flagg.id, harper.id]


@pytest.mark.asyncio
async def test_extract_board_paywalled_substack_post_raises(
    db_session: AsyncSession,
    news_source: NewsSource,
    monkeypatch,
) -> None:
    """A Substack post with ``audience=only_paid`` raises before Gemini is called."""
    assert news_source.id is not None
    item = await _make_news_item(
        db_session,
        source_id=news_source.id,
        url="https://example.substack.com/p/2026-nba-draft-big-board-paid",
    )

    fixture_payload = json.loads((_FIXTURE_DIR / "big_board_paid.json").read_text())

    async def _stub_http_get(url: str) -> str:
        return json.dumps(fixture_payload)

    monkeypatch.setattr(board_extraction_service, "_http_get", _stub_http_get)

    gemini_called = {"called": False}

    async def _gemini_should_not_be_called(article_text: str, *, client=None):
        gemini_called["called"] = True
        return ExtractedBoard(draft_year=2026, entries=[])

    monkeypatch.setattr(
        board_extraction_service,
        "_extract_via_gemini",
        _gemini_should_not_be_called,
    )

    assert item.id is not None
    with pytest.raises(PaywallDetectedError):
        await extract_board(db_session, news_item_id=item.id)

    assert gemini_called["called"] is False
    result = await db_session.execute(
        select(Board).where(Board.news_item_id == item.id)
    )
    assert result.scalar_one_or_none() is None


# --- Vector-resolution circuit breaker -------------------------------------


@pytest.mark.asyncio
async def test_extract_board_opens_vector_circuit_after_a_stall(
    db_session: AsyncSession,
    news_source: NewsSource,
    monkeypatch,
) -> None:
    """A stalled vector resolve disables vector search for the rest of the board.

    Regression guard for the "Extract Board" spinner that never resolved: the
    per-name embedding call had no ceiling, so one hung call (and, on a big
    board, one per remaining name) froze the whole synchronous request. Once a
    single resolve crosses the stall threshold, extraction stops calling the
    vector path — so the board still gets created instead of hanging.
    """
    assert news_source.id is not None
    item = await _make_news_item(db_session, source_id=news_source.id)

    # Make the breaker trip on the very first slow call.
    monkeypatch.setattr(
        board_extraction_service, "_RESOLUTION_STALL_THRESHOLD_SECONDS", 0.05
    )

    calls = {"count": 0}

    async def _slow_then_skipped(db, query: str, k: int = 5) -> list[Candidate]:
        calls["count"] += 1
        # First call stalls past the (patched) threshold; if the breaker works
        # it is never invoked again, so later names cost nothing.
        await asyncio.sleep(0.1)
        return []

    monkeypatch.setattr(
        board_extraction_service, "find_candidate_players", _slow_then_skipped
    )

    _stub_extraction(
        monkeypatch,
        ExtractedBoard(
            draft_year=2026,
            published_at=datetime(2026, 3, 15),
            entries=[
                ExtractedBoardEntry(player_name="Nobody One", rank=1),
                ExtractedBoardEntry(player_name="Nobody Two", rank=2),
                ExtractedBoardEntry(player_name="Nobody Three", rank=3),
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
    assert board.size == 3
    # Circuit opened after the first stalled resolve: the other two names
    # skipped the vector path entirely.
    assert calls["count"] == 1

    result = await db_session.execute(
        select(BoardEntry).where(BoardEntry.board_id == board.id)
    )
    entries = result.scalars().all()
    assert len(entries) == 3
    assert all(e.resolution_method == ResolutionMethod.UNRESOLVED for e in entries)
