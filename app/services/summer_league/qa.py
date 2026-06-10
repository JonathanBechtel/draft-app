"""QA validators for Summer League raw and normalized backbone data."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeagueDataQuality,
    SummerLeagueGame,
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

GAME_ENDPOINTS = (
    "boxscoretraditionalv2",
    "boxscoreadvancedv2",
    "boxscorescoringv2",
    "playbyplayv2",
    "shotchartdetail",
)
SEASON_ENDPOINTS = ("manifest", "leaguegamelog_team", "leaguegamelog_player")


class SummerLeagueQASeverity(str, Enum):
    """Severity levels used by Summer League QA findings."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class SummerLeagueSlice:
    """One Summer League season/venue slice to validate."""

    year: int
    league_id: str


@dataclass(frozen=True, slots=True)
class SummerLeagueQAFinding:
    """One structured QA finding."""

    severity: SummerLeagueQASeverity
    code: str
    message: str
    evidence: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class SummerLeagueQASnapshot:
    """Stable row-count snapshot for idempotency comparisons."""

    slices: tuple[SummerLeagueSlice, ...]
    table_counts: dict[str, int]


@dataclass(slots=True)
class SummerLeagueQAReport:
    """Aggregate Summer League QA report."""

    slices: tuple[SummerLeagueSlice, ...] = ()
    findings: list[SummerLeagueQAFinding] = field(default_factory=list)

    def add(self, finding: SummerLeagueQAFinding) -> None:
        """Append one QA finding."""
        self.findings.append(finding)

    def extend(self, findings: list[SummerLeagueQAFinding]) -> None:
        """Append multiple QA findings."""
        self.findings.extend(findings)

    @property
    def has_errors(self) -> bool:
        """Return whether the report contains any error-severity finding."""
        return any(
            finding.severity == SummerLeagueQASeverity.ERROR
            for finding in self.findings
        )

    @property
    def finding_codes(self) -> list[str]:
        """Return finding codes in report order."""
        return [finding.code for finding in self.findings]

    @property
    def severity_counts(self) -> dict[SummerLeagueQASeverity, int]:
        """Return finding counts keyed by severity."""
        counts: Counter[SummerLeagueQASeverity] = Counter(
            finding.severity for finding in self.findings
        )
        return {severity: counts[severity] for severity in SummerLeagueQASeverity}


def _finding(
    severity: SummerLeagueQASeverity,
    code: str,
    message: str,
    **evidence: Any,
) -> SummerLeagueQAFinding:
    return SummerLeagueQAFinding(
        severity=severity,
        code=code,
        message=message,
        evidence=evidence or None,
    )


async def validate_raw_audit_integrity(
    db: AsyncSession,
    *,
    slice_: SummerLeagueSlice,
) -> list[SummerLeagueQAFinding]:
    """Validate raw audit rows for one Summer League slice."""
    raw_run = await _load_raw_run(db, slice_)
    if raw_run is None or raw_run.id is None:
        return [
            _finding(
                SummerLeagueQASeverity.ERROR,
                "RAW_RUN_MISSING",
                "No audited Summer League raw run exists for this slice.",
                year=slice_.year,
                league_id=slice_.league_id,
            )
        ]

    findings: list[SummerLeagueQAFinding] = []
    raw_files = await _load_raw_files(db, raw_run.id)
    if raw_run.status != SummerLeagueRawRunStatus.COMPLETE:
        severity = (
            SummerLeagueQASeverity.ERROR
            if raw_run.status == SummerLeagueRawRunStatus.FAILED
            else SummerLeagueQASeverity.WARNING
        )
        findings.append(
            _finding(
                severity,
                "RAW_RUN_INCOMPLETE",
                "Raw audit run is not marked complete.",
                raw_run_id=raw_run.id,
                status=raw_run.status.value,
            )
        )
    if raw_run.error_count > 0:
        findings.append(
            _finding(
                SummerLeagueQASeverity.ERROR,
                "RAW_RUN_ERRORS_RECORDED",
                "Raw audit run recorded source fetch or parse errors.",
                raw_run_id=raw_run.id,
                error_count=raw_run.error_count,
            )
        )
    if not raw_run.manifest_sha256:
        findings.append(
            _finding(
                SummerLeagueQASeverity.WARNING,
                "RAW_MANIFEST_HASH_MISSING",
                "Raw run manifest is missing a SHA-256 hash.",
                raw_run_id=raw_run.id,
            )
        )

    expected_file_count = len(SEASON_ENDPOINTS) + raw_run.game_count * len(
        GAME_ENDPOINTS
    )
    if len(raw_files) != expected_file_count:
        findings.append(
            _finding(
                SummerLeagueQASeverity.ERROR,
                "RAW_FILE_COUNT_MISMATCH",
                "Audited raw file count does not match manifest game count.",
                raw_run_id=raw_run.id,
                expected=expected_file_count,
                actual=len(raw_files),
                game_count=raw_run.game_count,
            )
        )

    findings.extend(_validate_raw_file_duplicates(raw_files))
    for raw_file in raw_files:
        findings.extend(_validate_raw_file(raw_file))
    return findings


def _validate_raw_file_duplicates(
    raw_files: list[SummerLeagueRawFile],
) -> list[SummerLeagueQAFinding]:
    grouped: dict[tuple[str, str | None], list[SummerLeagueRawFile]] = {}
    for raw_file in raw_files:
        grouped.setdefault((raw_file.endpoint, raw_file.game_id), []).append(raw_file)

    findings: list[SummerLeagueQAFinding] = []
    for (endpoint, game_id), files in grouped.items():
        if len(files) <= 1:
            continue
        findings.append(
            _finding(
                SummerLeagueQASeverity.ERROR,
                "RAW_FILE_DUPLICATE_ENDPOINT",
                "Raw audit contains duplicate endpoint rows for the same scope.",
                endpoint=endpoint,
                game_id=game_id,
                count=len(files),
                raw_file_ids=[file.id for file in files],
                relative_paths=[file.relative_path for file in files],
            )
        )
    return findings


def _validate_raw_file(raw_file: SummerLeagueRawFile) -> list[SummerLeagueQAFinding]:
    findings: list[SummerLeagueQAFinding] = []
    evidence = {
        "raw_file_id": raw_file.id,
        "endpoint": raw_file.endpoint,
        "game_id": raw_file.game_id,
        "relative_path": raw_file.relative_path,
    }
    if raw_file.parse_status == SummerLeagueRawFileStatus.MISSING:
        findings.append(
            _finding(
                SummerLeagueQASeverity.ERROR,
                "RAW_FILE_MISSING",
                "Expected raw file is missing.",
                **evidence,
            )
        )
        return findings
    if raw_file.parse_status == SummerLeagueRawFileStatus.EMPTY:
        findings.append(
            _finding(
                SummerLeagueQASeverity.ERROR,
                "RAW_FILE_EMPTY",
                "Expected raw file is empty.",
                **evidence,
            )
        )
    if raw_file.parse_status == SummerLeagueRawFileStatus.PARSE_FAILED:
        findings.append(
            _finding(
                SummerLeagueQASeverity.ERROR,
                "RAW_FILE_PARSE_FAILED",
                "Expected raw file failed JSON/result-set parsing.",
                parse_error=raw_file.parse_error,
                **evidence,
            )
        )
    if raw_file.parse_status == SummerLeagueRawFileStatus.PARSED:
        if raw_file.row_count is None:
            findings.append(
                _finding(
                    SummerLeagueQASeverity.WARNING,
                    "RAW_FILE_ROW_COUNT_MISSING",
                    "Parsed raw file is missing a row count.",
                    **evidence,
                )
            )
        if not raw_file.sha256:
            findings.append(
                _finding(
                    SummerLeagueQASeverity.WARNING,
                    "RAW_FILE_HASH_MISSING",
                    "Parsed raw file is missing a SHA-256 hash.",
                    **evidence,
                )
            )
        if raw_file.byte_size is not None and raw_file.byte_size <= 0:
            findings.append(
                _finding(
                    SummerLeagueQASeverity.WARNING,
                    "RAW_FILE_BYTE_SIZE_INVALID",
                    "Parsed raw file has a non-positive byte size.",
                    byte_size=raw_file.byte_size,
                    **evidence,
                )
            )
    return findings


async def validate_normalization_parity(
    db: AsyncSession,
    *,
    slice_: SummerLeagueSlice,
) -> list[SummerLeagueQAFinding]:
    """Validate row-count parity between raw audit and normalized tables."""
    raw_run = await _load_raw_run(db, slice_)
    competition = await _load_competition(db, slice_)
    if competition is None or competition.id is None:
        if raw_run is None:
            return []
        return [
            _finding(
                SummerLeagueQASeverity.ERROR,
                "NORMALIZED_COMPETITION_MISSING",
                "Raw run exists but normalized competition row is missing.",
                year=slice_.year,
                league_id=slice_.league_id,
                raw_run_id=raw_run.id,
            )
        ]
    if raw_run is None:
        return [
            _finding(
                SummerLeagueQASeverity.ERROR,
                "NORMALIZATION_RAW_RUN_MISSING",
                "Normalized competition exists without an audited raw run.",
                competition_id=competition.id,
                year=slice_.year,
                league_id=slice_.league_id,
            )
        ]

    findings: list[SummerLeagueQAFinding] = []
    if competition.raw_run_id != raw_run.id:
        findings.append(
            _finding(
                SummerLeagueQASeverity.ERROR,
                "NORMALIZATION_RAW_RUN_LINK_MISMATCH",
                "Competition raw_run_id does not point at this slice's raw run.",
                competition_id=competition.id,
                competition_raw_run_id=competition.raw_run_id,
                expected_raw_run_id=raw_run.id,
            )
        )

    game_count = await _count_games(db, competition.id)
    if game_count != raw_run.game_count:
        findings.append(
            _finding(
                SummerLeagueQASeverity.ERROR,
                "NORMALIZATION_GAME_COUNT_MISMATCH",
                "Normalized game count does not match raw manifest game count.",
                competition_id=competition.id,
                expected=raw_run.game_count,
                actual=game_count,
            )
        )

    team_log_count = await _count_team_game_logs(db, competition.id)
    if team_log_count != raw_run.team_gamelog_rows:
        findings.append(
            _finding(
                SummerLeagueQASeverity.ERROR,
                "NORMALIZATION_TEAM_LOG_COUNT_MISMATCH",
                "Normalized team game-log count does not match raw team rows.",
                competition_id=competition.id,
                expected=raw_run.team_gamelog_rows,
                actual=team_log_count,
            )
        )

    player_log_count = await _count_player_game_logs(db, competition.id)
    if player_log_count != raw_run.player_gamelog_rows:
        findings.append(
            _finding(
                SummerLeagueQASeverity.WARNING,
                "NORMALIZATION_PLAYER_LOG_COUNT_MISMATCH",
                "Normalized player game-log count does not match raw player rows.",
                competition_id=competition.id,
                expected=raw_run.player_gamelog_rows,
                actual=player_log_count,
            )
        )
    return findings


async def validate_player_resolution(
    db: AsyncSession,
    *,
    slice_: SummerLeagueSlice,
) -> list[SummerLeagueQAFinding]:
    """Validate source-player resolution state and denormalized player links."""
    competition = await _load_competition(db, slice_)
    if competition is None or competition.id is None:
        return []

    findings: list[SummerLeagueQAFinding] = []
    rows = await _load_player_log_resolution_rows(db, competition.id)
    for source_player, player_log in rows:
        if source_player.canonical_player_id is None:
            if (
                source_player.resolution_status
                == SummerLeagueResolutionStatus.UNRESOLVED
            ):
                findings.append(
                    _finding(
                        SummerLeagueQASeverity.WARNING,
                        "PLAYER_UNRESOLVED",
                        "Source player has normalized logs but no canonical link.",
                        source_player_id=source_player.id,
                        nba_stats_person_id=source_player.nba_stats_person_id,
                        raw_player_name=source_player.raw_player_name,
                    )
                )
            if player_log.player_id is not None:
                findings.append(
                    _finding(
                        SummerLeagueQASeverity.ERROR,
                        "PLAYER_LOG_CANONICAL_LINK_MISMATCH",
                        "Player log has a canonical player but source player does not.",
                        source_player_id=source_player.id,
                        player_game_log_id=player_log.id,
                        player_id=player_log.player_id,
                    )
                )
            continue

        if source_player.resolution_status == SummerLeagueResolutionStatus.UNRESOLVED:
            findings.append(
                _finding(
                    SummerLeagueQASeverity.ERROR,
                    "PLAYER_RESOLUTION_INCONSISTENT",
                    "Source player has a canonical link but unresolved status.",
                    source_player_id=source_player.id,
                    canonical_player_id=source_player.canonical_player_id,
                )
            )
        if player_log.player_id is None:
            findings.append(
                _finding(
                    SummerLeagueQASeverity.ERROR,
                    "PLAYER_LOG_CANONICAL_LINK_MISSING",
                    "Resolved source player has a player log missing player_id.",
                    source_player_id=source_player.id,
                    player_game_log_id=player_log.id,
                    canonical_player_id=source_player.canonical_player_id,
                )
            )
        elif player_log.player_id != source_player.canonical_player_id:
            findings.append(
                _finding(
                    SummerLeagueQASeverity.ERROR,
                    "PLAYER_LOG_CANONICAL_LINK_MISMATCH",
                    "Player log player_id does not match source-player link.",
                    source_player_id=source_player.id,
                    player_game_log_id=player_log.id,
                    expected=source_player.canonical_player_id,
                    actual=player_log.player_id,
                )
            )

        if (
            source_player.resolution_status
            == SummerLeagueResolutionStatus.VECTOR_CANDIDATE
            and not source_player.resolution_candidates
        ):
            findings.append(
                _finding(
                    SummerLeagueQASeverity.WARNING,
                    "PLAYER_VECTOR_CANDIDATE_MISSING_EVIDENCE",
                    "Vector-candidate source player is missing candidate evidence.",
                    source_player_id=source_player.id,
                )
            )
    return findings


async def validate_historical_data_quality(
    db: AsyncSession,
    *,
    slices: list[SummerLeagueSlice],
) -> list[SummerLeagueQAFinding]:
    """Validate historical source-player ranges and data-quality flags."""
    findings: list[SummerLeagueQAFinding] = []
    for slice_ in slices:
        competition = await _load_competition(db, slice_)
        if competition is None or competition.id is None:
            continue
        findings.extend(_validate_competition_quality_flags(competition))
        source_players = await _load_source_players_for_competition(db, competition.id)
        for source_player in source_players:
            if (
                source_player.first_seen_year is not None
                and source_player.last_seen_year is not None
                and source_player.first_seen_year > source_player.last_seen_year
            ):
                findings.append(
                    _finding(
                        SummerLeagueQASeverity.ERROR,
                        "HISTORICAL_SOURCE_PLAYER_YEAR_RANGE_INVALID",
                        "Source player first_seen_year is after last_seen_year.",
                        source_player_id=source_player.id,
                        first_seen_year=source_player.first_seen_year,
                        last_seen_year=source_player.last_seen_year,
                    )
                )
            if (
                source_player.first_seen_year is not None
                and slice_.year < source_player.first_seen_year
            ) or (
                source_player.last_seen_year is not None
                and slice_.year > source_player.last_seen_year
            ):
                findings.append(
                    _finding(
                        SummerLeagueQASeverity.WARNING,
                        "HISTORICAL_SOURCE_PLAYER_SLICE_OUT_OF_RANGE",
                        "Source-player seen-year range excludes this competition.",
                        source_player_id=source_player.id,
                        year=slice_.year,
                        first_seen_year=source_player.first_seen_year,
                        last_seen_year=source_player.last_seen_year,
                    )
                )
    return findings


def _validate_competition_quality_flags(
    competition: SummerLeagueCompetition,
) -> list[SummerLeagueQAFinding]:
    expected_pbp = competition.data_quality == SummerLeagueDataQuality.FULL
    expected_shotchart = competition.data_quality == SummerLeagueDataQuality.FULL
    findings: list[SummerLeagueQAFinding] = []
    if competition.pbp_available != expected_pbp:
        findings.append(
            _finding(
                SummerLeagueQASeverity.WARNING,
                "DATA_QUALITY_FLAG_MISMATCH",
                "Competition pbp_available flag does not match data_quality.",
                competition_id=competition.id,
                data_quality=competition.data_quality.value,
                pbp_available=competition.pbp_available,
            )
        )
    if competition.shotchart_available != expected_shotchart:
        findings.append(
            _finding(
                SummerLeagueQASeverity.WARNING,
                "DATA_QUALITY_FLAG_MISMATCH",
                "Competition shotchart_available flag does not match data_quality.",
                competition_id=competition.id,
                data_quality=competition.data_quality.value,
                shotchart_available=competition.shotchart_available,
            )
        )
    return findings


async def validate_referential_integrity(
    db: AsyncSession,
    *,
    slice_: SummerLeagueSlice,
) -> list[SummerLeagueQAFinding]:
    """Validate cross-table references that DB foreign keys cannot fully express."""
    competition = await _load_competition(db, slice_)
    if competition is None or competition.id is None:
        return []

    findings: list[SummerLeagueQAFinding] = []
    if competition.raw_run_id is None:
        findings.append(
            _finding(
                SummerLeagueQASeverity.ERROR,
                "REFERENTIAL_COMPETITION_RAW_RUN_MISSING",
                "Competition is not linked to a raw audit run.",
                competition_id=competition.id,
            )
        )

    games = await _load_games(db, competition.id)
    teams_by_id = await _load_teams_by_id(db, competition.id)
    for game in games:
        if game.home_team_entry_id is None or game.away_team_entry_id is None:
            findings.append(
                _finding(
                    SummerLeagueQASeverity.WARNING,
                    "REFERENTIAL_GAME_TEAM_MISSING",
                    "Game is missing a home or away team reference.",
                    competition_id=competition.id,
                    game_id=game.id,
                    nba_stats_game_id=game.nba_stats_game_id,
                    home_team_entry_id=game.home_team_entry_id,
                    away_team_entry_id=game.away_team_entry_id,
                )
            )
            continue
        if (
            game.home_team_entry_id not in teams_by_id
            or game.away_team_entry_id not in teams_by_id
        ):
            findings.append(
                _finding(
                    SummerLeagueQASeverity.ERROR,
                    "REFERENTIAL_GAME_TEAM_COMPETITION_MISMATCH",
                    "Game references a team entry outside its competition.",
                    competition_id=competition.id,
                    game_id=game.id,
                    home_team_entry_id=game.home_team_entry_id,
                    away_team_entry_id=game.away_team_entry_id,
                )
            )

    findings.extend(await _validate_team_log_references(db, competition.id))
    findings.extend(await _validate_player_log_references(db, competition.id))
    return findings


async def capture_summer_league_snapshot(
    db: AsyncSession,
    *,
    slices: list[SummerLeagueSlice],
) -> SummerLeagueQASnapshot:
    """Capture row counts for idempotency checks before and after a run."""
    return SummerLeagueQASnapshot(
        slices=tuple(slices),
        table_counts={
            "raw_runs": await _count_raw_runs(db, slices),
            "raw_files": await _count_raw_files(db, slices),
            "competitions": await _count_competitions(db, slices),
            "team_entries": await _count_scoped_table(
                db, SummerLeagueTeamEntry, slices
            ),
            "games": await _count_scoped_table(db, SummerLeagueGame, slices),
            "team_game_logs": await _count_scoped_table(
                db, SummerLeagueTeamGameLog, slices
            ),
            "source_players": await _count_source_players(db, slices),
            "player_game_logs": await _count_scoped_table(
                db, SummerLeaguePlayerGameLog, slices
            ),
        },
    )


def compare_idempotency_snapshots(
    before: SummerLeagueQASnapshot,
    after: SummerLeagueQASnapshot,
) -> list[SummerLeagueQAFinding]:
    """Return findings for changed row counts across idempotency snapshots."""
    findings: list[SummerLeagueQAFinding] = []
    table_names = sorted(set(before.table_counts) | set(after.table_counts))
    for table_name in table_names:
        before_count = before.table_counts.get(table_name, 0)
        after_count = after.table_counts.get(table_name, 0)
        if before_count != after_count:
            findings.append(
                _finding(
                    SummerLeagueQASeverity.ERROR,
                    "IDEMPOTENCY_SNAPSHOT_CHANGED",
                    "Summer League row count changed across idempotency snapshots.",
                    table=table_name,
                    before=before_count,
                    after=after_count,
                )
            )
    return findings


async def run_summer_league_backbone_qa(
    db: AsyncSession,
    *,
    slices: list[SummerLeagueSlice],
) -> SummerLeagueQAReport:
    """Run reusable Summer League backbone QA validators for selected slices."""
    report = SummerLeagueQAReport(slices=tuple(slices))
    for slice_ in slices:
        report.extend(await validate_raw_audit_integrity(db, slice_=slice_))
        report.extend(await validate_normalization_parity(db, slice_=slice_))
        report.extend(await validate_player_resolution(db, slice_=slice_))
        report.extend(await validate_referential_integrity(db, slice_=slice_))
    report.extend(await validate_historical_data_quality(db, slices=slices))
    return report


async def _load_raw_run(
    db: AsyncSession,
    slice_: SummerLeagueSlice,
) -> SummerLeagueRawRun | None:
    result = await db.execute(
        select(SummerLeagueRawRun).where(
            SummerLeagueRawRun.year == slice_.year,  # type: ignore[arg-type]
            SummerLeagueRawRun.league_id == slice_.league_id,  # type: ignore[arg-type]
        )
    )
    return result.scalar_one_or_none()


async def _load_competition(
    db: AsyncSession,
    slice_: SummerLeagueSlice,
) -> SummerLeagueCompetition | None:
    result = await db.execute(
        select(SummerLeagueCompetition).where(
            SummerLeagueCompetition.year == slice_.year,  # type: ignore[arg-type]
            SummerLeagueCompetition.league_id == slice_.league_id,  # type: ignore[arg-type]
        )
    )
    return result.scalar_one_or_none()


async def _load_raw_files(
    db: AsyncSession,
    raw_run_id: int,
) -> list[SummerLeagueRawFile]:
    result = await db.execute(
        select(SummerLeagueRawFile).where(
            SummerLeagueRawFile.raw_run_id == raw_run_id  # type: ignore[arg-type]
        )
    )
    return list(result.scalars().all())


async def _load_games(
    db: AsyncSession,
    competition_id: int,
) -> list[SummerLeagueGame]:
    result = await db.execute(
        select(SummerLeagueGame).where(
            SummerLeagueGame.competition_id == competition_id  # type: ignore[arg-type]
        )
    )
    return list(result.scalars().all())


async def _load_teams_by_id(
    db: AsyncSession,
    competition_id: int,
) -> dict[int, SummerLeagueTeamEntry]:
    result = await db.execute(
        select(SummerLeagueTeamEntry).where(
            SummerLeagueTeamEntry.competition_id == competition_id  # type: ignore[arg-type]
        )
    )
    return {team.id: team for team in result.scalars().all() if team.id is not None}


async def _load_player_log_resolution_rows(
    db: AsyncSession,
    competition_id: int,
) -> list[tuple[SummerLeagueSourcePlayer, SummerLeaguePlayerGameLog]]:
    result = await db.execute(
        select(SummerLeagueSourcePlayer, SummerLeaguePlayerGameLog)
        .join(
            SummerLeaguePlayerGameLog,
            SummerLeaguePlayerGameLog.source_player_id == SummerLeagueSourcePlayer.id,  # type: ignore[arg-type]
        )
        .where(
            SummerLeaguePlayerGameLog.competition_id == competition_id  # type: ignore[arg-type]
        )
        .order_by(SummerLeagueSourcePlayer.id)  # type: ignore[arg-type]
    )
    return [(source_player, player_log) for source_player, player_log in result.all()]


async def _load_source_players_for_competition(
    db: AsyncSession,
    competition_id: int,
) -> list[SummerLeagueSourcePlayer]:
    result = await db.execute(
        select(SummerLeagueSourcePlayer)
        .join(
            SummerLeaguePlayerGameLog,
            SummerLeaguePlayerGameLog.source_player_id == SummerLeagueSourcePlayer.id,  # type: ignore[arg-type]
        )
        .where(
            SummerLeaguePlayerGameLog.competition_id == competition_id  # type: ignore[arg-type]
        )
        .distinct(SummerLeagueSourcePlayer.id)  # type: ignore[arg-type]
    )
    return list(result.scalars().all())


async def _count_raw_runs(
    db: AsyncSession,
    slices: list[SummerLeagueSlice],
) -> int:
    total = 0
    for slice_ in slices:
        total += await _scalar_count(
            db,
            select(func.count())
            .select_from(SummerLeagueRawRun)
            .where(
                SummerLeagueRawRun.year == slice_.year,  # type: ignore[arg-type]
                SummerLeagueRawRun.league_id == slice_.league_id,  # type: ignore[arg-type]
            ),
        )
    return total


async def _count_raw_files(
    db: AsyncSession,
    slices: list[SummerLeagueSlice],
) -> int:
    total = 0
    for slice_ in slices:
        total += await _scalar_count(
            db,
            select(func.count())
            .select_from(SummerLeagueRawFile)
            .where(
                SummerLeagueRawFile.year == slice_.year,  # type: ignore[arg-type]
                SummerLeagueRawFile.league_id == slice_.league_id,  # type: ignore[arg-type]
            ),
        )
    return total


async def _count_competitions(
    db: AsyncSession,
    slices: list[SummerLeagueSlice],
) -> int:
    total = 0
    for slice_ in slices:
        total += await _scalar_count(
            db,
            select(func.count())
            .select_from(SummerLeagueCompetition)
            .where(
                SummerLeagueCompetition.year == slice_.year,  # type: ignore[arg-type]
                SummerLeagueCompetition.league_id == slice_.league_id,  # type: ignore[arg-type]
            ),
        )
    return total


async def _count_source_players(
    db: AsyncSession,
    slices: list[SummerLeagueSlice],
) -> int:
    seen_ids: set[int] = set()
    for slice_ in slices:
        competition = await _load_competition(db, slice_)
        if competition is None or competition.id is None:
            continue
        for source_player in await _load_source_players_for_competition(
            db, competition.id
        ):
            if source_player.id is not None:
                seen_ids.add(source_player.id)
    return len(seen_ids)


async def _count_scoped_table(
    db: AsyncSession,
    table: type[SummerLeagueTeamEntry]
    | type[SummerLeagueGame]
    | type[SummerLeagueTeamGameLog]
    | type[SummerLeaguePlayerGameLog],
    slices: list[SummerLeagueSlice],
) -> int:
    total = 0
    for slice_ in slices:
        competition = await _load_competition(db, slice_)
        if competition is None or competition.id is None:
            continue
        total += await _scalar_count(
            db,
            select(func.count())
            .select_from(table)
            .where(
                table.competition_id == competition.id  # type: ignore[attr-defined, arg-type]
            ),
        )
    return total


async def _count_games(db: AsyncSession, competition_id: int) -> int:
    return await _scalar_count(
        db,
        select(func.count())
        .select_from(SummerLeagueGame)
        .where(
            SummerLeagueGame.competition_id == competition_id  # type: ignore[arg-type]
        ),
    )


async def _count_team_game_logs(db: AsyncSession, competition_id: int) -> int:
    return await _scalar_count(
        db,
        select(func.count())
        .select_from(SummerLeagueTeamGameLog)
        .where(
            SummerLeagueTeamGameLog.competition_id == competition_id  # type: ignore[arg-type]
        ),
    )


async def _count_player_game_logs(db: AsyncSession, competition_id: int) -> int:
    return await _scalar_count(
        db,
        select(func.count())
        .select_from(SummerLeaguePlayerGameLog)
        .where(
            SummerLeaguePlayerGameLog.competition_id == competition_id  # type: ignore[arg-type]
        ),
    )


async def _count_source_players_for_competition(
    db: AsyncSession,
    competition_id: int,
) -> int:
    result = await db.execute(
        select(func.count(func.distinct(SummerLeagueSourcePlayer.id)))
        .select_from(SummerLeagueSourcePlayer)
        .join(
            SummerLeaguePlayerGameLog,
            SummerLeaguePlayerGameLog.source_player_id == SummerLeagueSourcePlayer.id,  # type: ignore[arg-type]
        )
        .where(
            SummerLeaguePlayerGameLog.competition_id == competition_id  # type: ignore[arg-type]
        )
    )
    return int(result.scalar_one() or 0)


async def _validate_team_log_references(
    db: AsyncSession,
    competition_id: int,
) -> list[SummerLeagueQAFinding]:
    result = await db.execute(
        select(SummerLeagueTeamGameLog, SummerLeagueGame, SummerLeagueTeamEntry)
        .join(
            SummerLeagueGame,
            SummerLeagueGame.id == SummerLeagueTeamGameLog.game_id,  # type: ignore[arg-type]
        )
        .join(
            SummerLeagueTeamEntry,
            SummerLeagueTeamEntry.id == SummerLeagueTeamGameLog.team_entry_id,  # type: ignore[arg-type]
        )
        .where(
            SummerLeagueTeamGameLog.competition_id == competition_id  # type: ignore[arg-type]
        )
    )
    findings: list[SummerLeagueQAFinding] = []
    for team_log, game, team in result.all():
        if (
            game.competition_id != team_log.competition_id
            or team.competition_id != team_log.competition_id
        ):
            findings.append(
                _finding(
                    SummerLeagueQASeverity.ERROR,
                    "REFERENTIAL_TEAM_LOG_COMPETITION_MISMATCH",
                    "Team log references a game or team from another competition.",
                    team_game_log_id=team_log.id,
                    log_competition_id=team_log.competition_id,
                    game_competition_id=game.competition_id,
                    team_competition_id=team.competition_id,
                )
            )
    return findings


async def _validate_player_log_references(
    db: AsyncSession,
    competition_id: int,
) -> list[SummerLeagueQAFinding]:
    result = await db.execute(
        select(
            SummerLeaguePlayerGameLog,
            SummerLeagueGame,
            SummerLeagueTeamEntry,
            SummerLeagueSourcePlayer,
        )
        .join(
            SummerLeagueGame,
            SummerLeagueGame.id == SummerLeaguePlayerGameLog.game_id,  # type: ignore[arg-type]
        )
        .join(
            SummerLeagueTeamEntry,
            SummerLeagueTeamEntry.id == SummerLeaguePlayerGameLog.team_entry_id,  # type: ignore[arg-type]
        )
        .join(
            SummerLeagueSourcePlayer,
            SummerLeagueSourcePlayer.id == SummerLeaguePlayerGameLog.source_player_id,  # type: ignore[arg-type]
        )
        .where(
            SummerLeaguePlayerGameLog.competition_id == competition_id  # type: ignore[arg-type]
        )
    )
    findings: list[SummerLeagueQAFinding] = []
    for player_log, game, team, source_player in result.all():
        if (
            game.competition_id != player_log.competition_id
            or team.competition_id != player_log.competition_id
        ):
            findings.append(
                _finding(
                    SummerLeagueQASeverity.ERROR,
                    "REFERENTIAL_PLAYER_LOG_COMPETITION_MISMATCH",
                    "Player log references a game or team from another competition.",
                    player_game_log_id=player_log.id,
                    log_competition_id=player_log.competition_id,
                    game_competition_id=game.competition_id,
                    team_competition_id=team.competition_id,
                )
            )
        if player_log.nba_stats_person_id != source_player.nba_stats_person_id:
            findings.append(
                _finding(
                    SummerLeagueQASeverity.ERROR,
                    "REFERENTIAL_PLAYER_LOG_SOURCE_MISMATCH",
                    "Player log NBA Stats person ID does not match source player.",
                    player_game_log_id=player_log.id,
                    source_player_id=source_player.id,
                    log_nba_stats_person_id=player_log.nba_stats_person_id,
                    source_nba_stats_person_id=source_player.nba_stats_person_id,
                )
            )
        if (
            player_log.player_id is not None
            and source_player.canonical_player_id is not None
            and player_log.player_id != source_player.canonical_player_id
        ):
            findings.append(
                _finding(
                    SummerLeagueQASeverity.ERROR,
                    "REFERENTIAL_PLAYER_LOG_CANONICAL_MISMATCH",
                    "Player log canonical player differs from source-player link.",
                    player_game_log_id=player_log.id,
                    source_player_id=source_player.id,
                    log_player_id=player_log.player_id,
                    source_canonical_player_id=source_player.canonical_player_id,
                )
            )
    return findings


async def _scalar_count(db: AsyncSession, stmt: Any) -> int:
    result = await db.execute(stmt)
    return int(result.scalar_one() or 0)
