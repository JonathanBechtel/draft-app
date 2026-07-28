"""Database coverage for the Summer League metrics input watermark."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.summer_league import SummerLeagueCompetition
from app.services.summer_league.metrics_input import (
    calculate_metrics_input_watermark,
)


@pytest.mark.asyncio
async def test_idempotent_timestamp_touch_does_not_advance_input_watermark(
    db_session: AsyncSession,
) -> None:
    """Content, not routine ``updated_at`` churn, controls rebuild invalidation."""
    competition = SummerLeagueCompetition(
        year=2026,
        league_id="15",
        venue_slug="las_vegas",
        display_name="Las Vegas Summer League",
    )
    db_session.add(competition)
    await db_session.flush()

    baseline = await calculate_metrics_input_watermark(db_session)
    competition.updated_at = datetime(2026, 7, 27, 12, 0)
    await db_session.flush()
    timestamp_only = await calculate_metrics_input_watermark(db_session)

    competition.venue_slug = "las_vegas_updated"
    await db_session.flush()
    content_changed = await calculate_metrics_input_watermark(db_session)

    assert timestamp_only == baseline
    assert content_changed != baseline
