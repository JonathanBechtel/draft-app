"""Integration tests for board_auto_ingest_service.run_auto_ingest.

All tests run against the shared integration-test Postgres schema.  The
board-extraction AI call is stubbed so no network calls are made.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.boards import Board, BoardKind, BoardStatus
from app.schemas.news_items import BoardExtractionResult, NewsItem, NewsItemTag
from app.schemas.news_sources import NewsSource
from app.services.board_auto_ingest_service import AutoIngestReport, run_auto_ingest


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 5, 31, 12, 0, 0)
_RECENT = _NOW - timedelta(days=2)
_OLD = _NOW - timedelta(days=30)  # outside the default 7-day window


async def _make_news_item(
    db: AsyncSession,
    *,
    source_id: int,
    tag: NewsItemTag = NewsItemTag.BIG_BOARD,
    published_at: datetime | None = None,
    last_extraction_attempted_at: datetime | None = None,
    last_extraction_result: BoardExtractionResult | None = None,
    external_id: str | None = None,
    title: str = "Test Board Article",
    url: str = "https://example.substack.com/p/test-board",
) -> NewsItem:
    """Insert a NewsItem into the test DB and return it."""
    static_counter = getattr(_make_news_item, "_counter", 0) + 1
    _make_news_item._counter = static_counter  # type: ignore[attr-defined]
    item = NewsItem(
        source_id=source_id,
        external_id=external_id or f"test-item-{static_counter}",
        title=title,
        url=url,
        tag=tag,
        published_at=published_at or _RECENT,
        last_extraction_attempted_at=last_extraction_attempted_at,
        last_extraction_result=last_extraction_result,
    )
    db.add(item)
    await db.flush()
    return item


async def _make_board(
    db: AsyncSession,
    *,
    news_source_id: int,
    news_item_id: Optional[int] = None,
    kind: BoardKind = BoardKind.BIG_BOARD,
    status: BoardStatus = BoardStatus.PENDING,
    created_at: datetime | None = None,
) -> Board:
    """Insert a Board and return it.

    ``created_at`` defaults to one hour in the past so the board is clearly
    "pre-existing" when the auto-ingest worker inspects the timestamp.
    """
    board = Board(
        news_source_id=news_source_id,
        news_item_id=news_item_id,
        draft_year=2026,
        published_at=_RECENT,
        size=5,
        status=status,
        kind=kind,
        num_rounds=None if kind == BoardKind.BIG_BOARD else 1,
        created_at=created_at or (datetime.utcnow() - timedelta(hours=1)),
    )
    db.add(board)
    await db.flush()
    return board


def _stub_extract_board(return_value: Optional[Board]):
    """Return a patch context manager for board_extraction_service.extract_board."""
    return patch(
        "app.services.board_auto_ingest_service.board_extraction_service.extract_board",
        new_callable=AsyncMock,
        return_value=return_value,
    )


# ---------------------------------------------------------------------------
# Basic extraction: fresh item → PENDING board created
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fresh_big_board_item_gets_extracted(
    db_session: AsyncSession,
    news_source: NewsSource,
) -> None:
    """A recently-tagged BIG_BOARD item with no prior attempt is extracted.

    The worker calls extract_board, receives a PENDING board, and the
    report reflects one new board extracted.
    """
    assert news_source.id is not None
    item = await _make_news_item(
        db_session, source_id=news_source.id, tag=NewsItemTag.BIG_BOARD
    )
    await db_session.commit()

    # Board has created_at=now to simulate being freshly created by extract_board.
    pending_board = await _make_board(
        db_session,
        news_source_id=news_source.id,
        news_item_id=item.id,
        kind=BoardKind.BIG_BOARD,
        status=BoardStatus.PENDING,
        created_at=datetime.utcnow(),
    )
    await db_session.commit()

    with _stub_extract_board(pending_board):
        report: AutoIngestReport = await run_auto_ingest(
            db_session, lookback_days=7
        )

    assert report.scanned == 1
    assert report.extracted_boards == 1
    assert report.extracted_mocks == 0
    assert report.errors == []


@pytest.mark.asyncio
async def test_fresh_mock_draft_item_gets_extracted(
    db_session: AsyncSession,
    news_source: NewsSource,
) -> None:
    """A MOCK_DRAFT-tagged item is extracted with kind=MOCK_DRAFT."""
    assert news_source.id is not None
    item = await _make_news_item(
        db_session, source_id=news_source.id, tag=NewsItemTag.MOCK_DRAFT
    )
    await db_session.commit()

    # Board has created_at=now to simulate being freshly created by extract_board.
    pending_board = await _make_board(
        db_session,
        news_source_id=news_source.id,
        news_item_id=item.id,
        kind=BoardKind.MOCK_DRAFT,
        status=BoardStatus.PENDING,
        created_at=datetime.utcnow(),
    )
    await db_session.commit()

    with _stub_extract_board(pending_board):
        report = await run_auto_ingest(db_session, lookback_days=7)

    assert report.scanned == 1
    assert report.extracted_mocks == 1
    assert report.extracted_boards == 0


# ---------------------------------------------------------------------------
# Dedup: existing board states are counted correctly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_existing_approved_board_counted_as_skipped(
    db_session: AsyncSession,
    news_source: NewsSource,
) -> None:
    """When extract_board returns an APPROVED board, it is counted as skipped."""
    assert news_source.id is not None
    item = await _make_news_item(
        db_session, source_id=news_source.id, tag=NewsItemTag.BIG_BOARD
    )
    await db_session.commit()

    approved_board = await _make_board(
        db_session,
        news_source_id=news_source.id,
        news_item_id=item.id,
        status=BoardStatus.APPROVED,
    )
    await db_session.commit()

    with _stub_extract_board(approved_board):
        report = await run_auto_ingest(db_session, lookback_days=7)

    assert report.scanned == 1
    assert report.skipped_existing_approved == 1
    assert report.extracted_boards == 0


@pytest.mark.asyncio
async def test_existing_rejected_board_counted_as_skipped(
    db_session: AsyncSession,
    news_source: NewsSource,
) -> None:
    """When extract_board returns a REJECTED board, it is counted as skipped."""
    assert news_source.id is not None
    item = await _make_news_item(
        db_session, source_id=news_source.id, tag=NewsItemTag.BIG_BOARD
    )
    await db_session.commit()

    rejected_board = await _make_board(
        db_session,
        news_source_id=news_source.id,
        news_item_id=item.id,
        status=BoardStatus.REJECTED,
    )
    await db_session.commit()

    with _stub_extract_board(rejected_board):
        report = await run_auto_ingest(db_session, lookback_days=7)

    assert report.scanned == 1
    assert report.skipped_existing_rejected == 1
    assert report.extracted_boards == 0


@pytest.mark.asyncio
async def test_existing_pending_board_counted_as_skipped_pending(
    db_session: AsyncSession,
    news_source: NewsSource,
) -> None:
    """When extract_board returns a pre-existing PENDING board, it is a skip."""
    assert news_source.id is not None
    item = await _make_news_item(
        db_session, source_id=news_source.id, tag=NewsItemTag.BIG_BOARD
    )
    await db_session.commit()

    # First "extraction" creates a pending board; second run returns same board.
    existing_pending = await _make_board(
        db_session,
        news_source_id=news_source.id,
        news_item_id=item.id,
        status=BoardStatus.PENDING,
    )
    await db_session.commit()

    # Mark the item as already attempted successfully so it passes eligibility
    # and reaches extract_board (which returns the existing pending board).
    item.last_extraction_attempted_at = _NOW - timedelta(hours=2)
    item.last_extraction_result = BoardExtractionResult.SUCCESS
    await db_session.flush()
    await db_session.commit()

    with _stub_extract_board(existing_pending):
        report = await run_auto_ingest(db_session, lookback_days=7)

    assert report.scanned == 1
    assert report.skipped_existing_pending == 1
    assert report.extracted_boards == 0


# ---------------------------------------------------------------------------
# Idempotency: running twice
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_twice_is_idempotent(
    db_session: AsyncSession,
    news_source: NewsSource,
) -> None:
    """Running the worker twice for the same item produces correct dedup counts.

    First run: fresh item → extracted (1 board).
    Second run: item now has SUCCESS result → passes eligibility → extract_board
    returns existing PENDING board → skipped_existing_pending increments.
    """
    assert news_source.id is not None
    item = await _make_news_item(
        db_session, source_id=news_source.id, tag=NewsItemTag.BIG_BOARD
    )
    await db_session.commit()

    # Board created "just now" — simulates the fresh board returned by extract_board.
    fresh_board = await _make_board(
        db_session,
        news_source_id=news_source.id,
        news_item_id=item.id,
        status=BoardStatus.PENDING,
        created_at=datetime.utcnow(),
    )
    # Board created "in the past" — simulates the same board on a re-run (dedup path).
    old_board = await _make_board(
        db_session,
        news_source_id=news_source.id,
        news_item_id=item.id,
        status=BoardStatus.PENDING,
        created_at=datetime.utcnow() - timedelta(hours=2),
    )
    await db_session.commit()

    # First run — stub returns the fresh board (freshly created).
    with _stub_extract_board(fresh_board):
        report1 = await run_auto_ingest(db_session, lookback_days=7)

    assert report1.extracted_boards == 1
    assert report1.skipped_existing_pending == 0

    # After first run the item should now have extraction memory written.
    await db_session.refresh(item)
    assert item.last_extraction_attempted_at is not None
    assert item.last_extraction_result == BoardExtractionResult.SUCCESS

    # Second run — stub returns the "old" board (dedup: already existed).
    # SUCCESS from first run means item is still eligible but dedup fires.
    with _stub_extract_board(old_board):
        report2 = await run_auto_ingest(db_session, lookback_days=7)

    assert report2.scanned == 1
    assert report2.extracted_boards == 0
    assert report2.skipped_existing_pending == 1


# ---------------------------------------------------------------------------
# dry_run=True
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_counts_without_writing(
    db_session: AsyncSession,
    news_source: NewsSource,
) -> None:
    """dry_run=True reports eligible items but calls no extraction and writes no boards."""
    assert news_source.id is not None
    await _make_news_item(
        db_session, source_id=news_source.id, tag=NewsItemTag.BIG_BOARD
    )
    await _make_news_item(
        db_session,
        source_id=news_source.id,
        tag=NewsItemTag.MOCK_DRAFT,
        external_id="mock-item-dry",
    )
    await db_session.commit()

    with patch(
        "app.services.board_auto_ingest_service.board_extraction_service.extract_board",
        new_callable=AsyncMock,
    ) as mock_extract:
        report = await run_auto_ingest(db_session, lookback_days=7, dry_run=True)

    # Extraction should not have been called.
    mock_extract.assert_not_called()

    # Report still reflects what would have been done.
    assert report.scanned == 2
    # Each item would have been counted once in dry_run mode.
    assert report.extracted_boards + report.extracted_mocks == 2
    assert report.errors == []

    # No boards should have been written to the DB.
    stmt = select(Board)
    result = await db_session.execute(stmt)
    boards = result.scalars().all()
    assert len(boards) == 0


# ---------------------------------------------------------------------------
# Error handling: paywall, extraction error, transient error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paywalled_article_recorded_as_paywalled(
    db_session: AsyncSession,
    news_source: NewsSource,
) -> None:
    """A PaywallDetectedError is caught, recorded as PAYWALLED, not re-raised."""
    from app.services.board_extraction_service import PaywallDetectedError

    assert news_source.id is not None
    item = await _make_news_item(
        db_session, source_id=news_source.id, tag=NewsItemTag.BIG_BOARD
    )
    await db_session.commit()

    with patch(
        "app.services.board_auto_ingest_service.board_extraction_service.extract_board",
        new_callable=AsyncMock,
        side_effect=PaywallDetectedError("gated"),
    ):
        report = await run_auto_ingest(db_session, lookback_days=7)

    # Should not propagate as an error in the report (it's an expected outcome).
    assert report.errors == []
    assert report.extracted_boards == 0

    await db_session.refresh(item)
    assert item.last_extraction_result == BoardExtractionResult.PAYWALLED


@pytest.mark.asyncio
async def test_extraction_error_recorded_as_unresolvable(
    db_session: AsyncSession,
    news_source: NewsSource,
) -> None:
    """A non-retryable BoardExtractionError is recorded as UNRESOLVABLE."""
    from app.services.board_extraction_service import BoardExtractionError

    assert news_source.id is not None
    item = await _make_news_item(
        db_session, source_id=news_source.id, tag=NewsItemTag.BIG_BOARD
    )
    await db_session.commit()

    with patch(
        "app.services.board_auto_ingest_service.board_extraction_service.extract_board",
        new_callable=AsyncMock,
        side_effect=BoardExtractionError("no body found"),
    ):
        report = await run_auto_ingest(db_session, lookback_days=7)

    assert len(report.errors) == 1
    assert report.errors[0]["exception_type"] == "BoardExtractionError"

    await db_session.refresh(item)
    assert item.last_extraction_result == BoardExtractionResult.UNRESOLVABLE


@pytest.mark.asyncio
async def test_unexpected_error_recorded_as_transient(
    db_session: AsyncSession,
    news_source: NewsSource,
) -> None:
    """An unexpected exception is classified as TRANSIENT_ERROR."""
    assert news_source.id is not None
    item = await _make_news_item(
        db_session, source_id=news_source.id, tag=NewsItemTag.BIG_BOARD
    )
    await db_session.commit()

    with patch(
        "app.services.board_auto_ingest_service.board_extraction_service.extract_board",
        new_callable=AsyncMock,
        side_effect=RuntimeError("network blip"),
    ):
        report = await run_auto_ingest(db_session, lookback_days=7)

    assert len(report.errors) == 1
    assert report.errors[0]["exception_type"] == "RuntimeError"

    await db_session.refresh(item)
    assert item.last_extraction_result == BoardExtractionResult.TRANSIENT_ERROR


# ---------------------------------------------------------------------------
# Eligibility filters: date window, permanent failures, retry cool-downs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_item_outside_lookback_window_not_scanned(
    db_session: AsyncSession,
    news_source: NewsSource,
) -> None:
    """Items published more than lookback_days ago are excluded entirely."""
    assert news_source.id is not None
    # Old item: published 30 days ago, outside the 7-day window.
    await _make_news_item(
        db_session,
        source_id=news_source.id,
        tag=NewsItemTag.BIG_BOARD,
        published_at=_OLD,
    )
    await db_session.commit()

    with patch(
        "app.services.board_auto_ingest_service.board_extraction_service.extract_board",
        new_callable=AsyncMock,
    ) as mock_extract:
        report = await run_auto_ingest(db_session, lookback_days=7)

    mock_extract.assert_not_called()
    # The item is outside the window so it doesn't appear in scanned count
    # (the DB query already filters by published_at).
    assert report.scanned == 0


@pytest.mark.asyncio
async def test_no_entries_result_not_retried(
    db_session: AsyncSession,
    news_source: NewsSource,
) -> None:
    """An item with NO_ENTRIES result is not retried (permanent failure)."""
    assert news_source.id is not None
    item = await _make_news_item(
        db_session,
        source_id=news_source.id,
        tag=NewsItemTag.BIG_BOARD,
        last_extraction_attempted_at=datetime.utcnow() - timedelta(hours=2),
        last_extraction_result=BoardExtractionResult.NO_ENTRIES,
    )
    await db_session.commit()

    with patch(
        "app.services.board_auto_ingest_service.board_extraction_service.extract_board",
        new_callable=AsyncMock,
    ) as mock_extract:
        report = await run_auto_ingest(db_session, lookback_days=7)

    mock_extract.assert_not_called()
    assert report.scanned == 1  # item is within window and was fetched
    assert report.extracted_boards == 0


@pytest.mark.asyncio
async def test_unresolvable_result_not_retried(
    db_session: AsyncSession,
    news_source: NewsSource,
) -> None:
    """An item with UNRESOLVABLE result is not retried (permanent failure)."""
    assert news_source.id is not None
    await _make_news_item(
        db_session,
        source_id=news_source.id,
        tag=NewsItemTag.BIG_BOARD,
        last_extraction_attempted_at=datetime.utcnow() - timedelta(hours=2),
        last_extraction_result=BoardExtractionResult.UNRESOLVABLE,
    )
    await db_session.commit()

    with patch(
        "app.services.board_auto_ingest_service.board_extraction_service.extract_board",
        new_callable=AsyncMock,
    ) as mock_extract:
        report = await run_auto_ingest(db_session, lookback_days=7)

    mock_extract.assert_not_called()
    assert report.extracted_boards == 0


@pytest.mark.asyncio
async def test_transient_error_not_retried_within_cooldown(
    db_session: AsyncSession,
    news_source: NewsSource,
) -> None:
    """TRANSIENT_ERROR item is skipped when less than 1 hour has elapsed."""
    assert news_source.id is not None
    await _make_news_item(
        db_session,
        source_id=news_source.id,
        tag=NewsItemTag.BIG_BOARD,
        last_extraction_attempted_at=datetime.utcnow() - timedelta(minutes=10),
        last_extraction_result=BoardExtractionResult.TRANSIENT_ERROR,
    )
    await db_session.commit()

    with patch(
        "app.services.board_auto_ingest_service.board_extraction_service.extract_board",
        new_callable=AsyncMock,
    ) as mock_extract:
        report = await run_auto_ingest(db_session, lookback_days=7)

    mock_extract.assert_not_called()
    assert report.extracted_boards == 0


# ---------------------------------------------------------------------------
# Feature flag: CLI wrapper exits without running (tested via settings mock)
# ---------------------------------------------------------------------------


def test_feature_flag_false_means_cli_exits_without_running() -> None:
    """When BOARD_AUTO_INGEST_ENABLED is False the CLI wrapper exits 0.

    This is a lightweight behavioural check: we verify the flag value rather
    than subprocess the CLI, since the subprocess path requires a live DB and
    is covered by the broader integration suite.  The actual exit-path is in
    ``scripts/run_board_auto_ingest.py`` and is exercised by reading the flag
    from ``settings`` before calling ``run_auto_ingest``.
    """
    from app.config import settings

    # Default value must be False so the worker ships dormant.
    assert settings.board_auto_ingest_enabled is False


# ---------------------------------------------------------------------------
# Non-board tags are excluded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scouting_report_item_excluded(
    db_session: AsyncSession,
    news_source: NewsSource,
) -> None:
    """SCOUTING_REPORT items are never submitted for board extraction."""
    assert news_source.id is not None
    await _make_news_item(
        db_session,
        source_id=news_source.id,
        tag=NewsItemTag.SCOUTING_REPORT,
    )
    await db_session.commit()

    with patch(
        "app.services.board_auto_ingest_service.board_extraction_service.extract_board",
        new_callable=AsyncMock,
    ) as mock_extract:
        report = await run_auto_ingest(db_session, lookback_days=7)

    mock_extract.assert_not_called()
    # Item was not in the DB query results (tag filter in SQL).
    assert report.scanned == 0


# ---------------------------------------------------------------------------
# extraction_result=None returned by extract_board → NO_ENTRIES
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_board_returns_none_recorded_as_no_entries(
    db_session: AsyncSession,
    news_source: NewsSource,
) -> None:
    """When extract_board returns None, the item is recorded as NO_ENTRIES."""
    assert news_source.id is not None
    item = await _make_news_item(
        db_session, source_id=news_source.id, tag=NewsItemTag.BIG_BOARD
    )
    await db_session.commit()

    with _stub_extract_board(return_value=None):
        report = await run_auto_ingest(db_session, lookback_days=7)

    assert report.scanned == 1
    assert report.extracted_boards == 0
    assert report.errors == []

    await db_session.refresh(item)
    assert item.last_extraction_result == BoardExtractionResult.NO_ENTRIES
