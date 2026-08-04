"""Integration tests for the Summer League QA validation service."""

from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.player_affiliation import (
    AffiliationStatus,
    AffiliationType,
    PlayerAffiliation,
)
from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import (
    SummerLeagueEdition,
    SummerLeagueDataQuality,
    SummerLeagueGame,
    SummerLeagueGameStatus,
    SummerLeaguePlayerGameLog,
    SummerLeagueParticipation,
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
from app.services.summer_league.qa import (
    SummerLeagueQASeverity,
    SummerLeagueSlice,
    run_summer_league_backbone_qa,
)
from app.services.summer_league.roster_reconcile import ROSTER_SOURCE


def _raw_file(
    raw_run: SummerLeagueIngestionRun,
    *,
    endpoint: str,
    relative_path: str,
    game_id: str | None = None,
    parse_status: SummerLeagueRawFileStatus = SummerLeagueRawFileStatus.PARSED,
    row_count: int = 1,
) -> SummerLeagueSourceDocument:
    return SummerLeagueSourceDocument(
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
    raw_run = SummerLeagueIngestionRun(
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

    source_player = SummerLeagueSourceRecord(
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

    # Give the seeded player a roster-sourced announcement so reconcile
    # classifies them announced-and-played (excluded from both flagged
    # lists); otherwise this "fully valid" slice would trip
    # RECONCILE_PLAYED_NOT_ANNOUNCED.
    affiliation = PlayerAffiliation(
        player_id=player.id,
        affiliation_type=AffiliationType.SUMMER_LEAGUE_ROSTER,
        status=AffiliationStatus.ANNOUNCED,
        recorded_at=datetime.utcnow(),
        source=ROSTER_SOURCE,
    )
    db_session.add(affiliation)
    await db_session.flush()
    db_session.add(
        SummerLeagueParticipation(
            competition_id=competition.id or 0,
            team_entry_id=magic.id or 0,
            source_player_id=source_player.id or 0,
            player_id=player.id,
            affiliation_id=affiliation.id,
            stint_no=1,
            roster_status=AffiliationStatus.ANNOUNCED,
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
    raw_run = SummerLeagueIngestionRun(
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

    competition = SummerLeagueEdition(
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

    source_player = SummerLeagueSourceRecord(
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


async def _seed_slice_with_reconcile_findings(db_session: AsyncSession) -> None:
    """Seed an otherwise-valid 2025/13 slice with known DNP and late-add players.

    Mirrors ``_seed_valid_slice``'s raw/normalized backbone (so no unrelated
    blocking findings fire), but seeds three source players covering all
    three reconcile buckets:

    - announced-and-played (excluded from both flagged lists)
    - announced-not-played (roster participation, no box-score row -> DNP/cut)
    - played-not-announced (box-score row, no roster participation -> late-add)
    """
    raw_run = SummerLeagueIngestionRun(
        year=2025,
        league_id="13",
        venue_slug="california_classic",
        status=SummerLeagueRawRunStatus.COMPLETE,
        team_gamelog_rows=2,
        player_gamelog_rows=2,
        game_count=1,
        error_count=0,
        manifest_path="2025/13/manifest.json",
        manifest_sha256="manifest-sha",
    )
    db_session.add(raw_run)
    await db_session.flush()

    raw_files = [
        _raw_file(
            raw_run,
            endpoint="manifest",
            relative_path="2025/13/manifest.json",
            row_count=0,
        ),
        _raw_file(
            raw_run,
            endpoint="leaguegamelog_team",
            relative_path="2025/13/leaguegamelog_team.json",
            row_count=2,
        ),
        _raw_file(
            raw_run,
            endpoint="leaguegamelog_player",
            relative_path="2025/13/leaguegamelog_player.json",
            row_count=2,
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
                relative_path=f"2025/13/games/1522500001/{endpoint}.json",
                game_id="1522500001",
                row_count=2,
            )
        )
    db_session.add_all(raw_files)

    competition = SummerLeagueEdition(
        year=2025,
        league_id="13",
        venue_slug="california_classic",
        display_name="2025 California Classic",
        data_quality=SummerLeagueDataQuality.FULL,
        pbp_available=True,
        shotchart_available=True,
        raw_run_id=raw_run.id,
    )
    db_session.add(competition)
    await db_session.flush()

    warriors = SummerLeagueTeamEntry(
        competition_id=competition.id or 0,
        nba_stats_team_id="1610612744",
        raw_team_name="Golden State Warriors",
        raw_team_abbreviation="GSW",
        team_slug="golden-state-warriors",
    )
    kings = SummerLeagueTeamEntry(
        competition_id=competition.id or 0,
        nba_stats_team_id="1610612758",
        raw_team_name="Sacramento Kings",
        raw_team_abbreviation="SAC",
        team_slug="sacramento-kings",
    )
    db_session.add_all([warriors, kings])
    await db_session.flush()

    game = SummerLeagueGame(
        competition_id=competition.id or 0,
        nba_stats_game_id="1522500001",
        game_date=date(2025, 7, 13),
        home_team_entry_id=warriors.id,
        away_team_entry_id=kings.id,
        home_score=90,
        away_score=85,
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
                team_entry_id=warriors.id or 0,
                pts=90,
            ),
            SummerLeagueTeamGameLog(
                competition_id=competition.id or 0,
                game_id=game.id or 0,
                team_entry_id=kings.id or 0,
                pts=85,
            ),
        ]
    )

    announced_and_played = SummerLeagueSourceRecord(
        nba_stats_person_id="2650001",
        raw_player_name="Announced Played Prospect",
        normalized_name=_normalized_name_key("Announced Played Prospect"),
        resolution_status=SummerLeagueResolutionStatus.UNRESOLVED,
    )
    announced_not_played = SummerLeagueSourceRecord(
        nba_stats_person_id="2650002",
        raw_player_name="Announced DNP Prospect",
        normalized_name=_normalized_name_key("Announced DNP Prospect"),
        resolution_status=SummerLeagueResolutionStatus.UNRESOLVED,
    )
    played_not_announced = SummerLeagueSourceRecord(
        nba_stats_person_id="2650003",
        raw_player_name="Late Add Prospect",
        normalized_name=_normalized_name_key("Late Add Prospect"),
        resolution_status=SummerLeagueResolutionStatus.UNRESOLVED,
    )
    db_session.add_all(
        [announced_and_played, announced_not_played, played_not_announced]
    )
    await db_session.flush()

    # Flush each affiliation first so participation can reference its id.
    for source_player in (announced_and_played, announced_not_played):
        affiliation = PlayerAffiliation(
            player_id=None,
            affiliation_type=AffiliationType.SUMMER_LEAGUE_ROSTER,
            status=AffiliationStatus.ANNOUNCED,
            recorded_at=datetime.utcnow(),
            source=ROSTER_SOURCE,
        )
        db_session.add(affiliation)
        await db_session.flush()
        db_session.add(
            SummerLeagueParticipation(
                competition_id=competition.id or 0,
                team_entry_id=warriors.id or 0,
                source_player_id=source_player.id or 0,
                player_id=None,
                affiliation_id=affiliation.id,
                stint_no=1,
                roster_status=AffiliationStatus.ANNOUNCED,
            )
        )
    await db_session.flush()

    for source_player in (announced_and_played, played_not_announced):
        db_session.add(
            SummerLeaguePlayerGameLog(
                competition_id=competition.id or 0,
                game_id=game.id or 0,
                team_entry_id=warriors.id or 0,
                source_player_id=source_player.id or 0,
                player_id=None,
                nba_stats_person_id=source_player.nba_stats_person_id,
                raw_player_name=source_player.raw_player_name,
                minutes_seconds=900,
                pts=10,
            )
        )
    await db_session.flush()


@pytest.mark.asyncio
async def test_run_summer_league_backbone_qa_surfaces_reconcile_warnings(
    db_session: AsyncSession,
) -> None:
    """Reconcile findings surface as accepted warnings, not blocking errors.

    Seeds a competition with a known DNP/cut player (announced, never played)
    and a known late-add player (played, never announced) atop an otherwise
    valid backbone. Asserts both reconcile codes appear at WARNING severity,
    carry the expected counts, and do not flip the harness's blocking exit.
    """
    await _seed_slice_with_reconcile_findings(db_session)

    report = await run_summer_league_backbone_qa(
        db_session,
        slices=[SummerLeagueSlice(year=2025, league_id="13")],
    )

    reconcile_findings = {
        finding.code: finding
        for finding in report.findings
        if finding.code
        in {"RECONCILE_ANNOUNCED_NOT_PLAYED", "RECONCILE_PLAYED_NOT_ANNOUNCED"}
    }
    assert set(reconcile_findings) == {
        "RECONCILE_ANNOUNCED_NOT_PLAYED",
        "RECONCILE_PLAYED_NOT_ANNOUNCED",
    }
    for finding in reconcile_findings.values():
        assert finding.severity == SummerLeagueQASeverity.WARNING
        assert finding.evidence is not None
        assert finding.evidence["count"] == 1

    assert report.has_errors is False
