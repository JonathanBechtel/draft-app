"""Standalone cron runner for scheduled content ingestion.

This script is designed to run as a scheduled Fly.io machine,
executing the news, podcast, and video ingestion cycles directly without
going through the HTTP API. This avoids timeout issues and keeps the web
app responsive.

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

from app.services.news_ingestion_service import (
    run_ingestion_cycle as run_news_ingestion_cycle,
)
from app.services.podcast_ingestion_service import (
    run_ingestion_cycle as run_podcast_ingestion_cycle,
)
from app.services.player_enrichment_service import run_enrichment_sweep
from app.services.video_ingestion_service import (
    run_ingestion_cycle as run_video_ingestion_cycle,
)
from app.utils.db_async import SessionLocal, dispose_engine

# Configure logging for cron context
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("cron_runner")


async def _run_news_job() -> None:
    """Run news ingestion and log a concise summary."""
    async with SessionLocal() as db:
        result = await run_news_ingestion_cycle(db)

    logger.info(
        "News ingestion complete: %s sources, %s added, %s skipped",
        result.sources_processed,
        result.items_added,
        result.items_skipped,
    )
    for error in result.errors:
        logger.warning("News ingestion error: %s", error)


async def _run_podcast_job() -> None:
    """Run podcast ingestion and log a concise summary."""
    async with SessionLocal() as db:
        result = await run_podcast_ingestion_cycle(db)

    logger.info(
        "Podcast ingestion complete: %s shows, %s added, %s skipped",
        result.shows_processed,
        result.episodes_added,
        result.episodes_skipped,
    )
    for error in result.errors:
        logger.warning("Podcast ingestion error: %s", error)


async def _run_video_job() -> None:
    """Run video ingestion and log a concise summary."""
    async with SessionLocal() as db:
        result = await run_video_ingestion_cycle(db)

    logger.info(
        "Video ingestion complete: %s channels, %s added, %s skipped",
        result.channels_processed,
        result.videos_added,
        result.videos_skipped,
    )
    for error in result.errors:
        logger.warning("Video ingestion error: %s", error)


async def _run_enrichment_job() -> None:
    """Run stub player enrichment and log a concise summary."""
    result = await run_enrichment_sweep(SessionLocal)

    logger.info(
        "Enrichment complete: %s attempted, %s enriched, %s failed",
        result.players_attempted,
        result.players_enriched,
        result.players_failed,
    )
    for error in result.errors:
        logger.warning("Enrichment error: %s", error)


async def main() -> int:
    """Run the scheduled news, podcast, and video ingestion cycles.

    Returns:
        Exit code (0 for success, 1 for failure)
    """
    start_time = datetime.now(timezone.utc)
    logger.info("Starting scheduled content ingestion")
    failed = False

    try:
        try:
            await _run_news_job()
        except Exception as exc:
            failed = True
            logger.error("News ingestion failed: %s", exc, exc_info=True)

        try:
            await _run_podcast_job()
        except Exception as exc:
            failed = True
            logger.error("Podcast ingestion failed: %s", exc, exc_info=True)

        try:
            await _run_video_job()
        except Exception as exc:
            failed = True
            logger.error("Video ingestion failed: %s", exc, exc_info=True)

        # Enrichment runs last — non-critical, should not block ingestion
        try:
            await _run_enrichment_job()
        except Exception as exc:
            failed = True
            logger.error("Enrichment failed: %s", exc, exc_info=True)

        elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
        if failed:
            logger.error(
                "Scheduled content ingestion finished with failures in %.1fs", elapsed
            )
            return 1

        logger.info(
            "Scheduled content ingestion finished successfully in %.1fs", elapsed
        )
        return 0

    finally:
        await dispose_engine()


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
