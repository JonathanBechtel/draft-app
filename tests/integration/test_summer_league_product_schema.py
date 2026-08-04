"""Integration tests for Summer League normalized product schema contracts."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.summer_league import (
    SummerLeagueEdition,
    SummerLeagueDataQuality,
    SummerLeagueGame,
    SummerLeagueGameStatus,
    SummerLeaguePlayerGameLog,
    SummerLeagueIngestionRun,
    SummerLeagueRawRunStatus,
    SummerLeagueResolutionStatus,
    SummerLeagueSourceRecord,
    SummerLeagueTeamEntry,
)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _raw_run() -> SummerLeagueIngestionRun:
    return SummerLeagueIngestionRun(
        year=2024,
        league_id="15",
        venue_slug="las_vegas",
        status=SummerLeagueRawRunStatus.COMPLETE,
        team_gamelog_rows=2,
        player_gamelog_rows=3,
        game_count=1,
        error_count=0,
        manifest_path="2024/15/manifest.json",
    )


async def _seed_game_context(
    db_session: AsyncSession,
) -> tuple[SummerLeagueEdition, SummerLeagueTeamEntry, SummerLeagueGame]:
    raw_run = _raw_run()
    db_session.add(raw_run)
    await db_session.flush()
    assert raw_run.id is not None

    competition = SummerLeagueEdition(
        year=2024,
        league_id="15",
        venue_slug="las_vegas",
        display_name="2024 Las Vegas Summer League",
        starts_on=date(2024, 7, 12),
        ends_on=date(2024, 7, 22),
        data_quality=SummerLeagueDataQuality.FULL,
        pbp_available=True,
        shotchart_available=True,
        raw_run_id=raw_run.id,
    )
    db_session.add(competition)
    await db_session.flush()
    assert competition.id is not None

    team = SummerLeagueTeamEntry(
        competition_id=competition.id,
        nba_stats_team_id="1610612747",
        raw_team_name="Los Angeles Lakers",
        raw_team_abbreviation="LAL",
        team_slug="los-angeles-lakers",
        wins=3,
        losses=2,
    )
    db_session.add(team)
    await db_session.flush()
    assert team.id is not None

    game = SummerLeagueGame(
        competition_id=competition.id,
        nba_stats_game_id="1522400001",
        game_date=date(2024, 7, 12),
        home_team_entry_id=team.id,
        home_score=88,
        away_score=84,
        status=SummerLeagueGameStatus.FINAL,
        source_quality=SummerLeagueDataQuality.FULL,
    )
    db_session.add(game)
    await db_session.flush()
    assert game.id is not None
    return competition, team, game


@pytest.mark.asyncio
async def test_product_schema_persists_core_enums_and_nullable_links(
    db_session: AsyncSession,
) -> None:
    """Product rows persist enums and allow unresolved player game logs."""
    competition, team, game = await _seed_game_context(db_session)

    source_player = SummerLeagueSourceRecord(
        nba_stats_person_id="1630001",
        raw_player_name="Test Prospect",
        normalized_name="test prospect",
        first_seen_year=2024,
        last_seen_year=2024,
        resolution_status=SummerLeagueResolutionStatus.UNRESOLVED,
        resolution_candidates=[{"player_id": 1, "score": 0.72, "method": "VECTOR"}],
    )
    db_session.add(source_player)
    await db_session.flush()
    assert source_player.id is not None

    log = SummerLeaguePlayerGameLog(
        competition_id=competition.id,  # type: ignore[arg-type]
        game_id=game.id,  # type: ignore[arg-type]
        team_entry_id=team.id,  # type: ignore[arg-type]
        source_player_id=source_player.id,
        player_id=None,
        nba_stats_person_id="1630001",
        raw_player_name="Test Prospect",
        minutes_seconds=1234,
        pts=18,
        ast=4,
        source_endpoint="boxscoretraditionalv2",
    )
    db_session.add(log)
    await db_session.flush()
    await db_session.refresh(log)

    assert competition.data_quality == SummerLeagueDataQuality.FULL
    assert game.status == SummerLeagueGameStatus.FINAL
    assert source_player.resolution_status == SummerLeagueResolutionStatus.UNRESOLVED
    assert log.player_id is None
    assert log.source_player_id == source_player.id


@pytest.mark.asyncio
async def test_product_schema_requires_unique_nba_stats_game_id(
    db_session: AsyncSession,
) -> None:
    """NBA.com GAME_ID is globally unique in normalized Summer League games."""
    competition, _, _ = await _seed_game_context(db_session)
    duplicate = SummerLeagueGame(
        competition_id=competition.id,  # type: ignore[arg-type]
        nba_stats_game_id="1522400001",
        status=SummerLeagueGameStatus.FINAL,
        source_quality=SummerLeagueDataQuality.FULL,
    )
    db_session.add(duplicate)

    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio
async def test_product_schema_requires_unique_nba_stats_person_id(
    db_session: AsyncSession,
) -> None:
    """NBA.com PERSON_ID is globally unique in source players."""
    db_session.add(
        SummerLeagueSourceRecord(
            nba_stats_person_id="1630002",
            raw_player_name="Duplicate Person",
            normalized_name="duplicate person",
        )
    )
    await db_session.flush()
    db_session.add(
        SummerLeagueSourceRecord(
            nba_stats_person_id="1630002",
            raw_player_name="Duplicate Person Jr",
            normalized_name="duplicate person jr",
        )
    )

    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio
async def test_legacy_game_with_null_tip_datetime_remains_valid(
    db_session: AsyncSession,
) -> None:
    """Rows written before scoreboard ingest (ticket #515) keep a null tip_datetime."""
    competition, _, game = await _seed_game_context(db_session)

    assert game.tip_datetime is None

    fetched = await db_session.get(SummerLeagueGame, game.id)
    assert fetched is not None
    assert fetched.tip_datetime is None
    assert fetched.status == SummerLeagueGameStatus.FINAL
    # Untouched sibling column stays populated -- the migration didn't disturb it.
    assert fetched.competition_id == competition.id


@pytest.mark.asyncio
async def test_game_tip_datetime_and_in_progress_status_roundtrip(
    db_session: AsyncSession,
) -> None:
    """tip_datetime persists as UTC and IN_PROGRESS roundtrips through the enum column."""
    competition, team, _ = await _seed_game_context(db_session)
    tip = datetime(2026, 7, 12, 22, 0, 0)

    live_game = SummerLeagueGame(
        competition_id=competition.id,  # type: ignore[arg-type]
        nba_stats_game_id="1522400099",
        game_date=date(2026, 7, 12),
        home_team_entry_id=team.id,
        tip_datetime=tip,
        status=SummerLeagueGameStatus.IN_PROGRESS,
        source_quality=SummerLeagueDataQuality.FULL,
    )
    db_session.add(live_game)
    await db_session.flush()
    await db_session.refresh(live_game)

    assert live_game.id is not None
    assert live_game.status == SummerLeagueGameStatus.IN_PROGRESS
    assert live_game.tip_datetime == tip

    fetched = await db_session.get(SummerLeagueGame, live_game.id)
    assert fetched is not None
    assert fetched.status == SummerLeagueGameStatus.IN_PROGRESS
    assert fetched.tip_datetime == tip
