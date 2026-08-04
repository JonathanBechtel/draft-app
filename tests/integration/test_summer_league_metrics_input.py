"""Database coverage for the Summer League metrics input watermark."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import (
    SummerLeagueEdition,
    SummerLeagueGame,
    SummerLeaguePlayerGameLog,
    SummerLeagueSourceRecord,
    SummerLeagueTeamEntry,
)
from app.services.sources.summer_league.metrics_input import (
    calculate_metrics_input_watermark,
)
from tests.integration.conftest import make_player


@pytest.mark.asyncio
async def test_idempotent_timestamp_touch_does_not_advance_input_watermark(
    db_session: AsyncSession,
) -> None:
    """Content, not routine ``updated_at`` churn, controls rebuild invalidation."""
    competition = SummerLeagueEdition(
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


@pytest.mark.asyncio
async def test_out_of_band_game_log_edit_forces_the_next_rebuild(
    db_session: AsyncSession,
) -> None:
    """A direct normalized-log repair changes the gate's input watermark."""
    player: PlayerMaster = make_player("Watermark", "Repair")
    db_session.add(player)
    await db_session.flush()
    assert player.id is not None

    competition = SummerLeagueEdition(
        year=2026,
        league_id="15",
        venue_slug="watermark_repair",
        display_name="Watermark Repair League",
    )
    db_session.add(competition)
    await db_session.flush()
    assert competition.id is not None

    home = SummerLeagueTeamEntry(
        competition_id=competition.id,
        nba_stats_team_id="watermark-home",
        raw_team_name="Watermark Home",
        raw_team_abbreviation="WHM",
        team_slug="watermark-home",
    )
    away = SummerLeagueTeamEntry(
        competition_id=competition.id,
        nba_stats_team_id="watermark-away",
        raw_team_name="Watermark Away",
        raw_team_abbreviation="WAY",
        team_slug="watermark-away",
    )
    db_session.add_all([home, away])
    await db_session.flush()
    assert home.id is not None and away.id is not None

    source_player = SummerLeagueSourceRecord(
        nba_stats_person_id="watermark-person",
        raw_player_name="Watermark Repair",
        normalized_name="watermark repair",
        canonical_player_id=player.id,
    )
    db_session.add(source_player)
    await db_session.flush()
    assert source_player.id is not None

    game = SummerLeagueGame(
        competition_id=competition.id,
        nba_stats_game_id="watermark-game",
        home_team_entry_id=home.id,
        away_team_entry_id=away.id,
    )
    db_session.add(game)
    await db_session.flush()
    assert game.id is not None

    log = SummerLeaguePlayerGameLog(
        competition_id=competition.id,
        game_id=game.id,
        team_entry_id=home.id,
        source_player_id=source_player.id,
        player_id=player.id,
        nba_stats_person_id=source_player.nba_stats_person_id,
        raw_player_name="Watermark Repair",
        minutes_seconds=1800,
        pts=12,
    )
    db_session.add(log)
    await db_session.flush()

    before_repair = await calculate_metrics_input_watermark(db_session)
    # Simulate a manual repair script/direct SQL update that does not rely on
    # the ORM's timestamp bookkeeping to signal changed metric input.
    await db_session.execute(
        update(SummerLeaguePlayerGameLog)
        .where(SummerLeaguePlayerGameLog.id == log.id)
        .values(pts=24)
    )
    after_repair = await calculate_metrics_input_watermark(db_session)

    assert after_repair != before_repair
