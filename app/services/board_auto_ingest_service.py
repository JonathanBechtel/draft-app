"""Periodic worker that auto-extracts boards from eligible NewsItems.

Scans recently-ingested ``NewsItem`` records tagged ``BIG_BOARD`` or
``MOCK_DRAFT``, applies an eligibility filter using extraction-memory
fields, and invokes ``board_extraction_service.extract_board`` for each
eligible item.  Extracted boards land in ``PENDING`` status for normal
admin review — this worker does **not** bypass the approval gate.

The worker is controlled by ``settings.board_auto_ingest_enabled`` (default
``False``).  The CLI wrapper checks this flag and exits cleanly when the
feature is disabled; ``run_auto_ingest`` itself always executes when called
so tests and one-off manual runs are not gated by the flag.

Eligibility rules
-----------------
An item is eligible when:

- ``tag`` is ``BIG_BOARD`` or ``MOCK_DRAFT``
- ``published_at`` is within the lookback window
- AND one of:

  - ``last_extraction_attempted_at IS NULL`` (never tried)
  - ``last_extraction_result == TRANSIENT_ERROR`` and the last attempt was
    more than 1 hour ago  (transient retry)
  - ``last_extraction_result == PAYWALLED`` and the last attempt was more
    than 30 days ago  (paywall retry)

``NO_ENTRIES`` and ``UNRESOLVABLE`` are permanent — never retried.
``SUCCESS`` is handled by ``extract_board``'s existing dedup; the worker
still passes these through so the dedup counters in the report reflect reality.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Optional

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.boards import BoardKind, BoardStatus
from app.schemas.news_items import BoardExtractionResult, NewsItem, NewsItemTag
from app.services import board_extraction_service
from app.services.board_extraction_service import (
    BoardExtractionError,
    PaywallDetectedError,
)

logger = logging.getLogger(__name__)

# Tags that indicate a board-extraction-worthy article.
_BOARD_TAGS: frozenset[NewsItemTag] = frozenset(
    {NewsItemTag.BIG_BOARD, NewsItemTag.MOCK_DRAFT}
)

# Retry back-off windows.
_TRANSIENT_RETRY_AFTER: timedelta = timedelta(hours=1)
_PAYWALL_RETRY_AFTER: timedelta = timedelta(days=30)


@dataclass
class AutoIngestReport:
    """Per-run summary returned by ``run_auto_ingest``.

    All counter fields default to 0 so callers can safely add to them
    without checking for ``None``.
    """

    scanned: int = 0
    extracted_boards: int = 0
    extracted_mocks: int = 0
    skipped_existing_pending: int = 0
    skipped_existing_approved: int = 0
    skipped_existing_rejected: int = 0
    errors: list[dict] = field(default_factory=list)  # type: ignore[type-arg]


def _now_utc() -> datetime:
    """Return the current UTC time as a naive datetime (matches DB columns)."""
    return datetime.now(UTC).replace(tzinfo=None)


def is_eligible(
    item: NewsItem,
    *,
    lookback_cutoff: datetime,
    now: Optional[datetime] = None,
) -> bool:
    """Return True when the news item should be submitted for extraction.

    Pure function — no IO.  The caller supplies the lookback cutoff so the
    predicate is deterministic and easily unit-tested.

    Args:
        item: The ``NewsItem`` row to evaluate.
        lookback_cutoff: Items published before this datetime are excluded.
        now: Current UTC time (naive).  Defaults to the real clock; inject in
            tests for deterministic cool-down comparisons.

    Returns:
        ``True`` when extraction should be attempted.
    """
    # Tag filter.
    if item.tag not in _BOARD_TAGS:
        return False

    # Date window filter.
    if item.published_at < lookback_cutoff:
        return False

    # Extraction-memory filter.
    attempted_at = item.last_extraction_attempted_at
    result = item.last_extraction_result

    # Never tried → eligible.
    if attempted_at is None:
        return True

    # Permanent failures → never retry.
    if result in (BoardExtractionResult.NO_ENTRIES, BoardExtractionResult.UNRESOLVABLE):
        return False

    _now = now if now is not None else _now_utc()

    # Transient error → retry after cool-down.
    if result == BoardExtractionResult.TRANSIENT_ERROR:
        return (_now - attempted_at) >= _TRANSIENT_RETRY_AFTER

    # Paywalled → retry after long cool-down.
    if result == BoardExtractionResult.PAYWALLED:
        return (_now - attempted_at) >= _PAYWALL_RETRY_AFTER

    # SUCCESS or None result (shouldn't happen but treat as eligible so
    # extract_board's own dedup counters are updated).
    return True


async def _fetch_eligible_items(
    db: AsyncSession,
    *,
    lookback_cutoff: datetime,
) -> list[NewsItem]:
    """Query NewsItems in the lookback window tagged as board-extractable.

    Args:
        db: Async session; no open transaction required.
        lookback_cutoff: Exclude items published before this datetime.

    Returns:
        All matching ``NewsItem`` rows ordered by ``published_at`` ascending.
    """
    stmt = (
        select(NewsItem)
        .where(
            NewsItem.tag.in_(  # type: ignore[attr-defined]
                [t.value for t in _BOARD_TAGS]
            )
        )
        .where(NewsItem.published_at >= lookback_cutoff)  # type: ignore[arg-type]
        .order_by(NewsItem.published_at.asc())  # type: ignore[attr-defined]
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def _record_extraction_result(
    db: AsyncSession,
    *,
    news_item_id: int,
    result: BoardExtractionResult,
    attempted_at: datetime,
) -> None:
    """Persist extraction-memory fields on the ``NewsItem`` row.

    Commits immediately so this write is durable even if a later item closes
    the shared worker session before performing network I/O.

    Args:
        db: Async session.
        news_item_id: PK of the ``news_items`` row to update.
        result: The outcome to record.
        attempted_at: When the attempt started (naive UTC).
    """
    stmt = (
        update(NewsItem)
        .where(NewsItem.id == news_item_id)  # type: ignore[arg-type]
        .values(
            last_extraction_attempted_at=attempted_at,
            last_extraction_result=result,
        )
    )
    # Existing-board dedup performs read queries and can leave an implicit
    # transaction open. It owns no writes, so close it before starting the
    # durable extraction-memory transaction.
    if db.in_transaction():
        await db.close()
    async with db.begin():
        await db.execute(stmt)


async def run_auto_ingest(
    db: AsyncSession,
    *,
    lookback_days: int = 7,
    dry_run: bool = False,
) -> AutoIngestReport:
    """Scan recent board-tagged NewsItems and extract boards for eligible ones.

    The feature flag (``settings.board_auto_ingest_enabled``) is checked by
    the **CLI wrapper** — this function always runs when called directly so
    it is testable without toggling the flag.

    For each eligible item the worker attempts extraction for every relevant
    board kind implied by the item's tag:

    - ``BIG_BOARD`` tagged items → extract with ``kind=BIG_BOARD``.
    - ``MOCK_DRAFT`` tagged items → extract with ``kind=MOCK_DRAFT``.

    Dedup is handled inside ``extract_board``; re-running is idempotent.

    Args:
        db: Async session; the function manages its own transactions.
        lookback_days: Articles published more than this many days ago are
            excluded from the scan.
        dry_run: When ``True``, count eligible items and log intended actions
            but make no extraction calls and write no DB rows.

    Returns:
        An ``AutoIngestReport`` summarising the run.
    """
    report = AutoIngestReport()
    now = _now_utc()
    lookback_cutoff = now - timedelta(days=lookback_days)

    logger.info(
        "board.auto_ingest.start lookback_days=%d dry_run=%s cutoff=%s",
        lookback_days,
        dry_run,
        lookback_cutoff.isoformat(),
    )
    if db.in_transaction():
        raise RuntimeError(
            "run_auto_ingest requires a session with no active caller transaction."
        )

    # Fetch all candidate items in one query (eligibility is cheap to evaluate
    # in Python and the candidate set is small — at most a few dozen rows per week).
    async with db.begin():
        candidates = await _fetch_eligible_items(db, lookback_cutoff=lookback_cutoff)

    report.scanned = len(candidates)
    logger.info("board.auto_ingest.scanned count=%d", report.scanned)

    for item in candidates:
        if not is_eligible(item, lookback_cutoff=lookback_cutoff):
            logger.debug(
                "board.auto_ingest.skip news_item_id=%s reason=ineligible", item.id
            )
            continue

        # Determine which board kind to extract based on the item's tag.
        kind = (
            BoardKind.MOCK_DRAFT
            if item.tag == NewsItemTag.MOCK_DRAFT
            else BoardKind.BIG_BOARD
        )

        if dry_run:
            logger.info(
                "board.auto_ingest.dry_run news_item_id=%s kind=%s title=%r",
                item.id,
                kind.value,
                (item.title or "")[:60],
            )
            if kind == BoardKind.BIG_BOARD:
                report.extracted_boards += 1
            else:
                report.extracted_mocks += 1
            continue

        attempted_at = now
        extraction_result: Optional[BoardExtractionResult] = None

        try:
            board = await board_extraction_service.extract_board(
                db,
                news_item_id=item.id,  # type: ignore[arg-type]
                kind=kind,
                before_network_io=db.close,
            )

            if board is None:
                extraction_result = BoardExtractionResult.NO_ENTRIES
                logger.info(
                    "board.auto_ingest.no_entries news_item_id=%s kind=%s",
                    item.id,
                    kind.value,
                )
            elif board.status == BoardStatus.APPROVED:
                extraction_result = BoardExtractionResult.SUCCESS
                report.skipped_existing_approved += 1
                logger.info(
                    "board.auto_ingest.skip_approved board_id=%s news_item_id=%s",
                    board.id,
                    item.id,
                )
            elif board.status == BoardStatus.REJECTED:
                extraction_result = BoardExtractionResult.SUCCESS
                report.skipped_existing_rejected += 1
                logger.info(
                    "board.auto_ingest.skip_rejected board_id=%s news_item_id=%s",
                    board.id,
                    item.id,
                )
            elif board.status == BoardStatus.PENDING:
                # PENDING board: determine whether this was freshly created by
                # this run or was already in PENDING before we started.
                # ``extract_board`` returns the existing PENDING board unchanged
                # (dedup path) rather than replacing it, so we use the board's
                # ``created_at`` timestamp relative to this run's ``now`` to
                # distinguish new from existing.
                # Allow a 10-second slack to account for sub-second skew between
                # the DB server clock and our local ``now``.
                _is_new_board = board.created_at >= (
                    attempted_at - timedelta(seconds=10)
                )
                extraction_result = BoardExtractionResult.SUCCESS
                if _is_new_board:
                    if kind == BoardKind.BIG_BOARD:
                        report.extracted_boards += 1
                    else:
                        report.extracted_mocks += 1
                    logger.info(
                        "board.auto_ingest.extracted board_id=%s news_item_id=%s kind=%s",
                        board.id,
                        item.id,
                        kind.value,
                    )
                else:
                    report.skipped_existing_pending += 1
                    logger.info(
                        "board.auto_ingest.skip_pending board_id=%s news_item_id=%s",
                        board.id,
                        item.id,
                    )

        except PaywallDetectedError as exc:
            extraction_result = BoardExtractionResult.PAYWALLED
            logger.info("board.auto_ingest.paywalled news_item_id=%s: %s", item.id, exc)

        except BoardExtractionError as exc:
            # board_extraction_service wraps transient fetch/Gemini failures
            # (httpx network/5xx at _http_get, asyncio.TimeoutError at the Gemini
            # call) in BoardExtractionError via ``raise ... from exc``. Treat
            # those as TRANSIENT_ERROR so the retry-throttle re-attempts them
            # after the cooldown, rather than permanently blacklisting an article
            # over a temporary outage. Genuinely non-retryable errors (not a
            # Substack URL, empty body, malformed AI response — raised without a
            # transient cause) stay UNRESOLVABLE.
            if isinstance(exc.__cause__, (httpx.HTTPError, asyncio.TimeoutError)):
                extraction_result = BoardExtractionResult.TRANSIENT_ERROR
            else:
                extraction_result = BoardExtractionResult.UNRESOLVABLE
            report.errors.append(
                {
                    "news_item_id": item.id,
                    "exception_type": type(exc).__name__,
                    "message": str(exc),
                }
            )
            logger.warning(
                "board.auto_ingest.error news_item_id=%s kind=%s result=%s error=%s: %s",
                item.id,
                kind.value,
                extraction_result.value,
                type(exc).__name__,
                exc,
            )

        except Exception as exc:
            # Unexpected / transient errors (network, DB).
            extraction_result = BoardExtractionResult.TRANSIENT_ERROR
            report.errors.append(
                {
                    "news_item_id": item.id,
                    "exception_type": type(exc).__name__,
                    "message": str(exc),
                }
            )
            logger.error(
                "board.auto_ingest.transient_error news_item_id=%s kind=%s error=%s: %s",
                item.id,
                kind.value,
                type(exc).__name__,
                exc,
            )

        finally:
            if extraction_result is not None and item.id is not None:
                try:
                    await _record_extraction_result(
                        db,
                        news_item_id=item.id,
                        result=extraction_result,
                        attempted_at=attempted_at,
                    )
                except Exception as record_exc:
                    logger.error(
                        "board.auto_ingest.record_failed news_item_id=%s: %s",
                        item.id,
                        record_exc,
                    )

    logger.info(
        "board.auto_ingest.done scanned=%d extracted_boards=%d extracted_mocks=%d "
        "skipped_existing_pending=%d skipped_existing_approved=%d "
        "skipped_existing_rejected=%d errors=%d",
        report.scanned,
        report.extracted_boards,
        report.extracted_mocks,
        report.skipped_existing_pending,
        report.skipped_existing_approved,
        report.skipped_existing_rejected,
        len(report.errors),
    )
    return report
