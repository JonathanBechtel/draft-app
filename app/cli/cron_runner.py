"""Standalone cron runner for news ingestion.

This script is designed to run as a scheduled Fly.io machine,
executing the news ingestion cycle directly without going through
the HTTP API. This avoids timeout issues and keeps the web app
responsive.

Usage:
    python -m app.cron_runner

Exit codes:
    0 - Success
    1 - Failure (check logs for details)
"""

import asyncio
import logging
import sys
from datetime import datetime, timezone

from app.services.news_ingestion_service import run_ingestion_cycle
from app.utils.db_async import SessionLocal, dispose_engine

# Configure logging for cron context
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("cron_runner")


async def main() -> int:
    """Run the news ingestion cycle.

    Returns:
        Exit code (0 for success, 1 for failure)
    """
    start_time = datetime.now(timezone.utc)
    logger.info("Starting scheduled news ingestion")

    try:
        async with SessionLocal() as db:
            result = await run_ingestion_cycle(db)

        elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()

        logger.info(
            f"Ingestion complete in {elapsed:.1f}s: "
            f"{result.sources_processed} sources, "
            f"{result.items_added} added, "
            f"{result.items_skipped} skipped"
        )

        if result.errors:
            for error in result.errors:
                logger.warning(f"Ingestion error: {error}")

        return 0

    except Exception as e:
        elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
        logger.error(f"Ingestion failed after {elapsed:.1f}s: {e}", exc_info=True)
        return 1

    finally:
        await dispose_engine()


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
