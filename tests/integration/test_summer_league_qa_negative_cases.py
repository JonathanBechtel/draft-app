"""Negative-case integration tests for Summer League backbone QA."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.summer_league import (
    SummerLeagueEdition,
    SummerLeagueDataQuality,
    SummerLeagueGame,
    SummerLeagueGameStatus,
    SummerLeaguePlayerGameLog,
    SummerLeagueSourceDocument,
    SummerLeagueRawFileStatus,
    SummerLeagueIngestionRun,
    SummerLeagueRawRunStatus,
    SummerLeagueResolutionStatus,
    SummerLeagueSourceRecord,
    SummerLeagueTeamEntry,
    SummerLeagueTeamGameLog,
)
from app.services.player_mention_service import _normalized_name_key
from app.services.summer_league.audit import audit_summer_league_raw
from app.services.summer_league.qa import (
    SummerLeagueSlice,
    run_summer_league_backbone_qa,
)

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "summer_league"


def _db_case(name: str) -> dict[str, Any]:
    payload = json.loads((FIXTURE_ROOT / "db_negative_cases.json").read_text())
    return cast(dict[str, Any], payload[name])


async def _load_raw_run(
    db_session: AsyncSession, *, year: int, league_id: str
) -> SummerLeagueIngestionRun:
    raw_run = (
        await db_session.execute(
            select(SummerLeagueIngestionRun).where(
                SummerLeagueIngestionRun.year == year,  # type: ignore[arg-type]
                SummerLeagueIngestionRun.league_id == league_id,  # type: ignore[arg-type]
            )
        )
    ).scalar_one()
    if raw_run.id is None:
        raise RuntimeError("Seeded raw run did not receive an id")
    return raw_run


def _raw_file(
    raw_run: SummerLeagueIngestionRun,
    *,
    endpoint: str,
    relative_path: str,
    game_id: str | None = None,
) -> SummerLeagueSourceDocument:
    return SummerLeagueSourceDocument(
        raw_run_id=raw_run.id or 0,
        year=raw_run.year,
        league_id=raw_run.league_id,
        endpoint=endpoint,
        game_id=game_id,
        relative_path=relative_path,
        sha256=f"sha-{endpoint}-{relative_path}",
        byte_size=128,
        row_count=1,
        parse_status=SummerLeagueRawFileStatus.PARSED,
    )


@pytest.mark.asyncio
async def test_qa_reports_corrupt_raw_files_and_missing_endpoints(
    db_session: AsyncSession,
) -> None:
    """Corrupt JSON and absent endpoint files produce raw QA finding codes."""
    await audit_summer_league_raw(
        db_session,
        raw_root=FIXTURE_ROOT / "corrupt",
        year=2024,
        league_id="15",
    )
    await audit_summer_league_raw(
        db_session,
        raw_root=FIXTURE_ROOT / "missing_endpoint",
        year=2007,
        league_id="15",
    )

    report = await run_summer_league_backbone_qa(
        db_session,
        slices=[
            SummerLeagueSlice(year=2024, league_id="15"),
            SummerLeagueSlice(year=2007, league_id="15"),
        ],
    )

    assert set(report.finding_codes) >= {
        "RAW_RUN_INCOMPLETE",
        "RAW_FILE_PARSE_FAILED",
        "RAW_FILE_MISSING",
        "NORMALIZED_COMPETITION_MISSING",
    }
    assert report.has_errors is True


@pytest.mark.asyncio
async def test_qa_reports_count_mismatches_and_unresolved_players(
    db_session: AsyncSession,
) -> None:
    """Raw/normalized count drift and unresolved player logs are named findings."""
    await audit_summer_league_raw(
        db_session,
        raw_root=FIXTURE_ROOT / "modern",
        year=2024,
        league_id="15",
    )
    raw_run = await _load_raw_run(db_session, year=2024, league_id="15")
    raw_run.game_count = 2
    raw_run.team_gamelog_rows = 3
    raw_run.player_gamelog_rows = 2

    competition = SummerLeagueEdition(
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
        status=SummerLeagueGameStatus.FINAL,
        source_quality=SummerLeagueDataQuality.FULL,
    )
    db_session.add(game)
    await db_session.flush()

    db_session.add(
        SummerLeagueTeamGameLog(
            competition_id=competition.id or 0,
            game_id=game.id or 0,
            team_entry_id=magic.id or 0,
            pts=106,
        )
    )
    unresolved = _db_case("unresolved_player")
    source_player = SummerLeagueSourceRecord(
        nba_stats_person_id=str(unresolved["nba_stats_person_id"]),
        raw_player_name=str(unresolved["raw_player_name"]),
        normalized_name=_normalized_name_key(str(unresolved["raw_player_name"])),
        first_seen_year=2024,
        last_seen_year=2024,
        resolution_status=SummerLeagueResolutionStatus.UNRESOLVED,
    )
    db_session.add(source_player)
    await db_session.flush()
    db_session.add(
        SummerLeaguePlayerGameLog(
            competition_id=competition.id or 0,
            game_id=game.id or 0,
            team_entry_id=magic.id or 0,
            source_player_id=source_player.id or 0,
            nba_stats_person_id=str(unresolved["nba_stats_person_id"]),
            raw_player_name=str(unresolved["raw_player_name"]),
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
        "NORMALIZATION_GAME_COUNT_MISMATCH",
        "NORMALIZATION_TEAM_LOG_COUNT_MISMATCH",
        "NORMALIZATION_PLAYER_LOG_COUNT_MISMATCH",
        "PLAYER_UNRESOLVED",
    }


@pytest.mark.asyncio
async def test_qa_reports_duplicate_raw_file_rows(
    db_session: AsyncSession,
) -> None:
    """Duplicate raw audit rows for one endpoint scope emit a duplicate code."""
    duplicate_case = _db_case("duplicate_raw_files")
    raw_run = SummerLeagueIngestionRun(
        year=int(duplicate_case["year"]),
        league_id=str(duplicate_case["league_id"]),
        venue_slug=str(duplicate_case["venue_slug"]),
        status=SummerLeagueRawRunStatus.COMPLETE,
        team_gamelog_rows=1,
        player_gamelog_rows=1,
        game_count=0,
        error_count=0,
        manifest_path="2024/15/manifest.json",
        manifest_sha256="manifest-sha",
    )
    db_session.add(raw_run)
    await db_session.flush()

    endpoint = str(duplicate_case["endpoint"])
    relative_paths = cast(list[str], duplicate_case["relative_paths"])
    db_session.add_all(
        [
            _raw_file(
                raw_run,
                endpoint="manifest",
                relative_path="2024/15/manifest.json",
            ),
            _raw_file(
                raw_run,
                endpoint="leaguegamelog_team",
                relative_path="2024/15/leaguegamelog_team.json",
            ),
            _raw_file(raw_run, endpoint=endpoint, relative_path=relative_paths[0]),
            _raw_file(raw_run, endpoint=endpoint, relative_path=relative_paths[1]),
        ]
    )
    await db_session.flush()

    report = await run_summer_league_backbone_qa(
        db_session,
        slices=[SummerLeagueSlice(year=2024, league_id="15")],
    )

    assert "RAW_FILE_DUPLICATE_ENDPOINT" in report.finding_codes
    assert report.has_errors is True


@pytest.mark.asyncio
async def test_qa_reports_orphaned_normalized_rows(
    db_session: AsyncSession,
) -> None:
    """Normalized rows without the right parent slice links emit orphan codes."""
    orphan_case = _db_case("orphan_competition")
    target = SummerLeagueEdition(
        year=int(orphan_case["year"]),
        league_id=str(orphan_case["league_id"]),
        venue_slug=str(orphan_case["venue_slug"]),
        display_name="2024 California Classic",
        data_quality=SummerLeagueDataQuality.PARTIAL,
        pbp_available=False,
        shotchart_available=False,
        raw_run_id=None,
    )
    other = SummerLeagueEdition(
        year=2024,
        league_id="99",
        venue_slug="other_fixture",
        display_name="Other Fixture",
        data_quality=SummerLeagueDataQuality.PARTIAL,
        pbp_available=False,
        shotchart_available=False,
        raw_run_id=None,
    )
    db_session.add_all([target, other])
    await db_session.flush()

    outside_team = SummerLeagueTeamEntry(
        competition_id=other.id or 0,
        nba_stats_team_id="1610612758",
        raw_team_name="Sacramento Kings",
        raw_team_abbreviation="SAC",
        team_slug="sacramento-kings",
    )
    outside_game = SummerLeagueGame(
        competition_id=other.id or 0,
        nba_stats_game_id="9922400001",
        game_date=date(2024, 7, 6),
        home_team_entry_id=None,
        away_team_entry_id=None,
        status=SummerLeagueGameStatus.FINAL,
        source_quality=SummerLeagueDataQuality.PARTIAL,
    )
    db_session.add_all([outside_team, outside_game])
    await db_session.flush()

    target_game = SummerLeagueGame(
        competition_id=target.id or 0,
        nba_stats_game_id="1322400001",
        game_date=date(2024, 7, 6),
        home_team_entry_id=outside_team.id,
        away_team_entry_id=outside_team.id,
        status=SummerLeagueGameStatus.FINAL,
        source_quality=SummerLeagueDataQuality.PARTIAL,
    )
    db_session.add(target_game)
    await db_session.flush()
    db_session.add(
        SummerLeagueTeamGameLog(
            competition_id=target.id or 0,
            game_id=outside_game.id or 0,
            team_entry_id=outside_team.id or 0,
            pts=86,
        )
    )
    await db_session.flush()

    report = await run_summer_league_backbone_qa(
        db_session,
        slices=[SummerLeagueSlice(year=2024, league_id="13")],
    )

    assert set(report.finding_codes) >= {
        "RAW_RUN_MISSING",
        "NORMALIZATION_RAW_RUN_MISSING",
        "REFERENTIAL_COMPETITION_RAW_RUN_MISSING",
        "REFERENTIAL_GAME_TEAM_COMPETITION_MISMATCH",
        "REFERENTIAL_TEAM_LOG_COMPETITION_MISMATCH",
    }
