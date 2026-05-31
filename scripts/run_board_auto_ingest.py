r"""CLI wrapper for the board auto-ingest worker.

Checks ``settings.board_auto_ingest_enabled`` before running. When the
feature flag is ``False`` (the default), exits 0 with a log message so
cron runs are silent and safe.  When enabled, invokes
``run_auto_ingest`` and logs a per-run summary.

Usage::

    # Normal cron invocation (reads .env for DATABASE_URL etc.):
    scripts/with-db-env.sh conda run -n draftguru --no-capture-output \
        python scripts/run_board_auto_ingest.py

    # Dry run (no DB writes, no extraction API calls):
    scripts/with-db-env.sh conda run -n draftguru --no-capture-output \
        python scripts/run_board_auto_ingest.py --dry-run

    # Override lookback window:
    scripts/with-db-env.sh conda run -n draftguru --no-capture-output \
        python scripts/run_board_auto_ingest.py --lookback-days 14

Environment variables
---------------------
- ``DATABASE_URL`` -- required (loaded from .env via with-db-env.sh).
- ``BOARD_AUTO_INGEST_ENABLED`` -- set to ``true`` to enable the worker.
- ``BOARD_AUTO_INGEST_LOOKBACK_DAYS`` -- override the lookback window.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from dotenv import load_dotenv

# Ensure repo root is on sys.path so ``app`` is importable when the script is
# executed directly.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

load_dotenv()

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from app.config import settings  # noqa: E402
from app.services.board_auto_ingest_service import run_auto_ingest  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Board auto-ingest worker: extract boards from recent BIG_BOARD/MOCK_DRAFT articles."
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=None,
        help=(
            "Override the lookback window (days). Defaults to "
            "BOARD_AUTO_INGEST_LOOKBACK_DAYS from settings."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate the run without calling extraction or writing the DB.",
    )
    return parser.parse_args()


async def main(*, lookback_days: int, dry_run: bool) -> None:
    """Run the auto-ingest worker with a fresh DB session.

    Args:
        lookback_days: Articles published more than this many days ago are
            excluded from the scan.
        dry_run: When ``True``, count eligible items and log intended actions
            but do not call extraction or write any DB rows.
    """
    database_url = os.getenv("DATABASE_URL") or settings.database_url
    if not database_url:
        logger.error("DATABASE_URL is not set — cannot connect to Postgres.")
        sys.exit(1)

    engine = create_async_engine(database_url, echo=False)
    session_factory = async_sessionmaker(
        bind=engine, expire_on_commit=False, class_=AsyncSession
    )

    async with session_factory() as db:
        report = await run_auto_ingest(db, lookback_days=lookback_days, dry_run=dry_run)

    await engine.dispose()

    logger.info(
        "auto_ingest summary: scanned=%d extracted_boards=%d extracted_mocks=%d "
        "skipped_pending=%d skipped_approved=%d skipped_rejected=%d errors=%d",
        report.scanned,
        report.extracted_boards,
        report.extracted_mocks,
        report.skipped_existing_pending,
        report.skipped_existing_approved,
        report.skipped_existing_rejected,
        len(report.errors),
    )

    if report.errors:
        for err in report.errors:
            logger.warning(
                "  error news_item_id=%s %s: %s",
                err.get("news_item_id"),
                err.get("exception_type"),
                err.get("message"),
            )


if __name__ == "__main__":
    args = _parse_args()

    if not settings.board_auto_ingest_enabled:
        logger.info(
            "board auto-ingest disabled via config (BOARD_AUTO_INGEST_ENABLED=false); "
            "exiting."
        )
        sys.exit(0)

    lookback = (
        args.lookback_days
        if args.lookback_days is not None
        else settings.board_auto_ingest_lookback_days
    )

    asyncio.run(main(lookback_days=lookback, dry_run=args.dry_run))
