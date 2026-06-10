"""Integration tests for the Summer League QA validation service."""

from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeagueDataQuality,
    SummerLeagueGame,
    SummerLeagueGameStatus,
    SummerLeaguePlayerGameLog,
    SummerLeagueRawFile,
    SummerLeagueRawFileStatus,
    SummerLeagueRawRun,
    SummerLeagueRawRunStatus,
    SummerLeagueResolutionStatus,
    SummerLeagueSourcePlayer,
    SummerLeagueTeamEntry,
    SummerLeagueTeamGameLog,
)
from app.services.player_mention_service import _normalized_name_key
from app.services.summer_league.qa import (
    SummerLeagueSlice,
    run_summer_league_backbone_qa,
)


def _raw_file(
    raw_run: SummerLeagueRawRun,
    *,
    endpoint: str,
    relative_path: str,
    game_id: str | None = None,
    parse_status: SummerLeagueRawFileStatus = SummerLeagueRawFileStatus.PARSED,
    row_count: int = 1,
) -> SummerLeagueRawFile:
    return SummerLeagueRawFile(
        raw_run_id=raw_run.id or 0,
        year=raw_run.year,
        league_id=raw_run.league_id,
        endpoint=endpoint,
        game_id=game_id,
        relative_path=relative_path,
        sha256=f"sha-{endpoint}-{game_id or 'season'}",
        byte_size=128,
        row_count=row_count,
        parse_status=parse_status,
    )


async def _seed_valid_slice(db_session: AsyncSession) -> None:
    raw_run = SummerLeagueRawRun(
        year=2024,
        league_id="15",
        venue_slug="las_vegas",
        status=SummerLeagueRawRunStatus.COMPLETE,
        team_gamelog_rows=2,
        player_gamelog_rows=1,
        game_count=1,
        error_count=0,
        manifest_path="2024/15/manifest.json",
        manifest_sha256="manifest-sha",
    )
    db_session.add(raw_run)
    await db_session.flush()

    raw_files = [
        _raw_file(
            raw_run,
            endpoint="manifest",
            relative_path="2024/15/manifest.json",
            row_count=0,
        ),
        _raw_file(
            raw_run,
            endpoint="leaguegamelog_team",
            relative_path="2024/15/leaguegamelog_team.json",
            row_count=2,
        ),
        _raw_file(
            raw_run,
            endpoint="leaguegamelog_player",
            relative_path="2024/15/leaguegamelog_player.json",
            row_count=1,
        ),
    ]
    for endpoint in (
        "boxscoretraditionalv2",
        "boxscoreadvancedv2",
        "boxscorescoringv2",
        "playbyplayv2",
        "shotchartdetail",
    ):
        raw_files.append(
            _raw_file(
                raw_run,
                endpoint=endpoint,
                relative_path=f"2024/15/games/1522400001/{endpoint}.json",
                game_id="1522400001",
                row_count=2,
            )
        )
    db_session.add_all(raw_files)

    competition = SummerLeagueCompetition(
        year=2024,
        league_id="15",
        venue_slug="las_vegas",
        display_name="2024 Las Vegas Summer League",
        data_quality=SummerLeagueDataQuality.FULL,
        pbp_available=True,
        shotchart_available=True,
        raw_run_id=raw_run.id,
    )
    db_session.add(competition)
    await db_session.flush()

    magic = SummerLeagueTeamEntry(
        competition_id=competition.id or 0,
        nba_stats_team_id="1610612753",
        raw_team_name="Orlando Magic",
        raw_team_abbreviation="ORL",
        team_slug="orlando-magic",
    )
    cavs = SummerLeagueTeamEntry(
        competition_id=competition.id or 0,
        nba_stats_team_id="1610612739",
        raw_team_name="Cleveland Cavaliers",
        raw_team_abbreviation="CLE",
        team_slug="cleveland-cavaliers",
    )
    db_session.add_all([magic, cavs])
    await db_session.flush()

    game = SummerLeagueGame(
        competition_id=competition.id or 0,
        nba_stats_game_id="1522400001",
        game_date=date(2024, 7, 12),
        home_team_entry_id=magic.id,
        away_team_entry_id=cavs.id,
        home_score=106,
        away_score=79,
        status=SummerLeagueGameStatus.FINAL,
        source_quality=SummerLeagueDataQuality.FULL,
    )
    db_session.add(game)
    await db_session.flush()

    db_session.add_all(
        [
            SummerLeagueTeamGameLog(
                competition_id=competition.id or 0,
                game_id=game.id or 0,
                team_entry_id=magic.id or 0,
                pts=106,
            ),
            SummerLeagueTeamGameLog(
                competition_id=competition.id or 0,
                game_id=game.id or 0,
                team_entry_id=cavs.id or 0,
                pts=79,
            ),
        ]
    )

    player = PlayerMaster(display_name="Resolved Prospect")
    db_session.add(player)
    await db_session.flush()

    source_player = SummerLeagueSourcePlayer(
        nba_stats_person_id="1640001",
        raw_player_name="Resolved Prospect",
        normalized_name=_normalized_name_key("Resolved Prospect"),
        first_seen_year=2024,
        last_seen_year=2024,
        canonical_player_id=player.id,
        resolution_status=SummerLeagueResolutionStatus.EXACT,
        resolution_confidence=1.0,
        resolved_at=datetime.utcnow(),
        resolved_by="test",
    )
    db_session.add(source_player)
    await db_session.flush()

    db_session.add(
        SummerLeaguePlayerGameLog(
            competition_id=competition.id or 0,
            game_id=game.id or 0,
            team_entry_id=magic.id or 0,
            source_player_id=source_player.id or 0,
            player_id=player.id,
            nba_stats_person_id="1640001",
            raw_player_name="Resolved Prospect",
            minutes_seconds=1200,
            pts=17,
        )
    )
    await db_session.flush()


@pytest.mark.asyncio
async def test_run_summer_league_backbone_qa_accepts_valid_slice(
    db_session: AsyncSession,
) -> None:
    """A complete and internally consistent slice produces no QA findings."""
    await _seed_valid_slice(db_session)

    report = await run_summer_league_backbone_qa(
        db_session,
        slices=[SummerLeagueSlice(year=2024, league_id="15")],
    )

    assert report.findings == []
    assert report.has_errors is False


@pytest.mark.asyncio
async def test_run_summer_league_backbone_qa_reports_expected_invalid_codes(
    db_session: AsyncSession,
) -> None:
    """Invalid raw, normalized, resolution, and reference rows emit QA codes."""
    raw_run = SummerLeagueRawRun(
        year=2024,
        league_id="15",
        venue_slug="las_vegas",
        status=SummerLeagueRawRunStatus.COMPLETE,
        team_gamelog_rows=2,
        player_gamelog_rows=2,
        game_count=1,
        error_count=0,
        manifest_path="2024/15/manifest.json",
        manifest_sha256="manifest-sha",
    )
    db_session.add(raw_run)
    await db_session.flush()
    db_session.add_all(
        [
            _raw_file(
                raw_run,
                endpoint="manifest",
                relative_path="2024/15/manifest.json",
                row_count=0,
            ),
            _raw_file(
                raw_run,
                endpoint="boxscoretraditionalv2",
                relative_path="2024/15/games/1522400001/boxscoretraditionalv2.json",
                game_id="1522400001",
                parse_status=SummerLeagueRawFileStatus.PARSE_FAILED,
            ),
        ]
    )

    competition = SummerLeagueCompetition(
        year=2024,
        league_id="15",
        venue_slug="las_vegas",
        display_name="2024 Las Vegas Summer League",
        data_quality=SummerLeagueDataQuality.FULL,
        pbp_available=False,
        shotchart_available=False,
        raw_run_id=raw_run.id,
    )
    db_session.add(competition)
    await db_session.flush()

    team = SummerLeagueTeamEntry(
        competition_id=competition.id or 0,
        nba_stats_team_id="1610612753",
        raw_team_name="Orlando Magic",
        raw_team_abbreviation="ORL",
        team_slug="orlando-magic",
    )
    db_session.add(team)
    await db_session.flush()

    game = SummerLeagueGame(
        competition_id=competition.id or 0,
        nba_stats_game_id="1522400001",
        game_date=date(2024, 7, 12),
        home_team_entry_id=None,
        away_team_entry_id=None,
        status=SummerLeagueGameStatus.FINAL,
        source_quality=SummerLeagueDataQuality.FULL,
    )
    db_session.add(game)
    await db_session.flush()

    source_player = SummerLeagueSourcePlayer(
        nba_stats_person_id="1640001",
        raw_player_name="Unresolved Prospect",
        normalized_name=_normalized_name_key("Unresolved Prospect"),
        first_seen_year=2025,
        last_seen_year=2024,
        resolution_status=SummerLeagueResolutionStatus.UNRESOLVED,
    )
    db_session.add(source_player)
    await db_session.flush()

    db_session.add(
        SummerLeaguePlayerGameLog(
            competition_id=competition.id or 0,
            game_id=game.id or 0,
            team_entry_id=team.id or 0,
            source_player_id=source_player.id or 0,
            nba_stats_person_id="1640001",
            raw_player_name="Unresolved Prospect",
            minutes_seconds=900,
            pts=8,
        )
    )
    await db_session.flush()

    report = await run_summer_league_backbone_qa(
        db_session,
        slices=[SummerLeagueSlice(year=2024, league_id="15")],
    )

    assert set(report.finding_codes) >= {
        "RAW_FILE_COUNT_MISMATCH",
        "RAW_FILE_PARSE_FAILED",
        "NORMALIZATION_TEAM_LOG_COUNT_MISMATCH",
        "NORMALIZATION_PLAYER_LOG_COUNT_MISMATCH",
        "PLAYER_UNRESOLVED",
        "REFERENTIAL_GAME_TEAM_MISSING",
        "DATA_QUALITY_FLAG_MISMATCH",
        "HISTORICAL_SOURCE_PLAYER_YEAR_RANGE_INVALID",
    }
    assert report.has_errors is True
