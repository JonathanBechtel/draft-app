"""Unit tests for Summer League QA report DTOs and snapshot helpers."""

from __future__ import annotations

from typing import Any

import pytest

from app.services.summer_league.qa import (
    SummerLeagueQAFinding,
    SummerLeagueQAReport,
    SummerLeagueQASnapshot,
    SummerLeagueQASeverity,
    SummerLeagueSlice,
    compare_idempotency_snapshots,
)
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
)
from app.services.summer_league import qa as service


class _FakeScalarResult:
    def __init__(self, values: list[Any]) -> None:
        self._values = values

    def all(self) -> list[Any]:
        return self._values


class _FakeResult:
    def __init__(
        self,
        *,
        scalar: Any = None,
        scalars: list[Any] | None = None,
        rows: list[tuple[Any, ...]] | None = None,
    ) -> None:
        self._scalar = scalar
        self._scalars = scalars or []
        self._rows = rows or []

    def scalar_one_or_none(self) -> Any:
        return self._scalar

    def scalar_one(self) -> Any:
        return self._scalar

    def scalars(self) -> _FakeScalarResult:
        return _FakeScalarResult(self._scalars)

    def all(self) -> list[tuple[Any, ...]]:
        return self._rows


class _FakeDb:
    def __init__(self, results: list[_FakeResult]) -> None:
        self.results = results

    async def execute(self, stmt: Any) -> _FakeResult:
        return self.results.pop(0)


def test_qa_report_aggregates_findings_and_severity_counts() -> None:
    """Report aggregation preserves finding order and exposes severity counts."""
    report = SummerLeagueQAReport(slices=(SummerLeagueSlice(2024, "15"),))

    report.add(
        SummerLeagueQAFinding(
            severity=SummerLeagueQASeverity.WARNING,
            code="PLAYER_UNRESOLVED",
            message="Player needs review.",
        )
    )
    report.extend(
        [
            SummerLeagueQAFinding(
                severity=SummerLeagueQASeverity.ERROR,
                code="RAW_FILE_MISSING",
                message="Raw file missing.",
                evidence={"relative_path": "2024/15/missing.json"},
            ),
            SummerLeagueQAFinding(
                severity=SummerLeagueQASeverity.INFO,
                code="QA_NOTE",
                message="Informational note.",
            ),
        ]
    )

    assert report.finding_codes == [
        "PLAYER_UNRESOLVED",
        "RAW_FILE_MISSING",
        "QA_NOTE",
    ]
    assert report.has_errors is True
    assert report.severity_counts == {
        SummerLeagueQASeverity.INFO: 1,
        SummerLeagueQASeverity.WARNING: 1,
        SummerLeagueQASeverity.ERROR: 1,
    }


def test_empty_qa_report_has_zero_severity_counts() -> None:
    """Empty reports remain non-error reports with zeroed severity counters."""
    report = SummerLeagueQAReport()

    assert report.has_errors is False
    assert report.finding_codes == []
    assert report.severity_counts == {
        SummerLeagueQASeverity.INFO: 0,
        SummerLeagueQASeverity.WARNING: 0,
        SummerLeagueQASeverity.ERROR: 0,
    }


def test_compare_idempotency_snapshots_reports_changed_table_counts() -> None:
    """Snapshot comparison flags table-count drift with structured evidence."""
    qa_slice = SummerLeagueSlice(2024, "15")
    before = SummerLeagueQASnapshot(
        slices=(qa_slice,),
        table_counts={"games": 1, "player_game_logs": 2},
    )
    after = SummerLeagueQASnapshot(
        slices=(qa_slice,),
        table_counts={"games": 1, "player_game_logs": 3},
    )

    findings = compare_idempotency_snapshots(before, after)

    assert len(findings) == 1
    assert findings[0].severity == SummerLeagueQASeverity.ERROR
    assert findings[0].code == "IDEMPOTENCY_SNAPSHOT_CHANGED"
    assert findings[0].evidence == {
        "table": "player_game_logs",
        "before": 2,
        "after": 3,
    }


def test_compare_idempotency_snapshots_returns_no_findings_for_stable_counts() -> None:
    """Snapshot comparison emits no findings when all row counts are stable."""
    qa_slice = SummerLeagueSlice(2024, "15")
    snapshot = SummerLeagueQASnapshot(
        slices=(qa_slice,),
        table_counts={"games": 1, "player_game_logs": 2},
    )

    assert compare_idempotency_snapshots(snapshot, snapshot) == []


def test_raw_file_duplicate_validator_reports_duplicate_endpoint_scope() -> None:
    """Duplicate raw file rows for one endpoint/game scope are named findings."""
    findings = service._validate_raw_file_duplicates(
        [
            _raw_file(
                SummerLeagueRawFileStatus.PARSED,
                id=20,
                endpoint="leaguegamelog_player",
                game_id=None,
                relative_path="2024/15/leaguegamelog_player.json",
            ),
            _raw_file(
                SummerLeagueRawFileStatus.PARSED,
                id=21,
                endpoint="leaguegamelog_player",
                game_id=None,
                relative_path="2024/15/leaguegamelog_player-copy.json",
            ),
        ]
    )

    assert len(findings) == 1
    assert findings[0].code == "RAW_FILE_DUPLICATE_ENDPOINT"
    assert findings[0].evidence == {
        "endpoint": "leaguegamelog_player",
        "game_id": None,
        "count": 2,
        "raw_file_ids": [20, 21],
        "relative_paths": [
            "2024/15/leaguegamelog_player.json",
            "2024/15/leaguegamelog_player-copy.json",
        ],
    }


def _raw_run(**overrides: object) -> SummerLeagueRawRun:
    data: dict[str, object] = {
        "id": 10,
        "year": 2024,
        "league_id": "15",
        "venue_slug": "las_vegas",
        "status": SummerLeagueRawRunStatus.COMPLETE,
        "team_gamelog_rows": 2,
        "player_gamelog_rows": 2,
        "game_count": 1,
        "error_count": 0,
        "manifest_path": "2024/15/manifest.json",
        "manifest_sha256": "sha",
    }
    data.update(overrides)
    return SummerLeagueRawRun(**data)


def _raw_file(
    parse_status: SummerLeagueRawFileStatus,
    **overrides: object,
) -> SummerLeagueRawFile:
    data: dict[str, object] = {
        "id": 20,
        "raw_run_id": 10,
        "year": 2024,
        "league_id": "15",
        "endpoint": "boxscoretraditionalv2",
        "game_id": "1522400001",
        "relative_path": "2024/15/games/1522400001/boxscoretraditionalv2.json",
        "parse_status": parse_status,
        "sha256": "sha",
        "byte_size": 100,
        "row_count": 1,
    }
    data.update(overrides)
    return SummerLeagueRawFile(**data)


def _competition(**overrides: object) -> SummerLeagueCompetition:
    data: dict[str, object] = {
        "id": 30,
        "year": 2024,
        "league_id": "15",
        "venue_slug": "las_vegas",
        "display_name": "2024 Las Vegas Summer League",
        "data_quality": SummerLeagueDataQuality.FULL,
        "pbp_available": True,
        "shotchart_available": True,
        "raw_run_id": 10,
    }
    data.update(overrides)
    return SummerLeagueCompetition(**data)


def _source_player(**overrides: object) -> SummerLeagueSourcePlayer:
    data: dict[str, object] = {
        "id": 40,
        "nba_stats_person_id": "1640001",
        "raw_player_name": "Source Player",
        "normalized_name": "source player",
        "resolution_status": SummerLeagueResolutionStatus.UNRESOLVED,
    }
    data.update(overrides)
    return SummerLeagueSourcePlayer(**data)


def _player_log(**overrides: object) -> SummerLeaguePlayerGameLog:
    data: dict[str, object] = {
        "id": 50,
        "competition_id": 30,
        "game_id": 60,
        "team_entry_id": 70,
        "source_player_id": 40,
        "player_id": None,
        "nba_stats_person_id": "1640001",
        "raw_player_name": "Source Player",
    }
    data.update(overrides)
    return SummerLeaguePlayerGameLog(**data)


@pytest.mark.asyncio
async def test_raw_audit_integrity_reports_run_and_file_findings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Raw audit validation reports incomplete runs and bad file metadata."""
    qa_slice = SummerLeagueSlice(2024, "15")
    raw_run = _raw_run(
        status=SummerLeagueRawRunStatus.PARTIAL,
        error_count=2,
        manifest_sha256=None,
    )
    raw_files = [
        _raw_file(SummerLeagueRawFileStatus.MISSING),
        _raw_file(SummerLeagueRawFileStatus.EMPTY, id=21),
        _raw_file(
            SummerLeagueRawFileStatus.PARSE_FAILED,
            id=22,
            parse_error="bad json",
        ),
        _raw_file(
            SummerLeagueRawFileStatus.PARSED,
            id=23,
            sha256=None,
            row_count=None,
            byte_size=0,
        ),
    ]

    async def fake_raw_run(
        _db: object, slice_: SummerLeagueSlice
    ) -> SummerLeagueRawRun:
        return raw_run

    async def fake_raw_files(_db: object, raw_run_id: int) -> list[SummerLeagueRawFile]:
        return raw_files

    monkeypatch.setattr(service, "_load_raw_run", fake_raw_run)
    monkeypatch.setattr(service, "_load_raw_files", fake_raw_files)

    findings = await service.validate_raw_audit_integrity(object(), slice_=qa_slice)  # type: ignore[arg-type]

    assert set(finding.code for finding in findings) >= {
        "RAW_RUN_INCOMPLETE",
        "RAW_RUN_ERRORS_RECORDED",
        "RAW_MANIFEST_HASH_MISSING",
        "RAW_FILE_COUNT_MISMATCH",
        "RAW_FILE_MISSING",
        "RAW_FILE_EMPTY",
        "RAW_FILE_PARSE_FAILED",
        "RAW_FILE_ROW_COUNT_MISSING",
        "RAW_FILE_HASH_MISSING",
        "RAW_FILE_BYTE_SIZE_INVALID",
    }


@pytest.mark.asyncio
async def test_raw_audit_integrity_treats_optional_detail_gaps_as_warnings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Historical PBP/shotchart endpoint gaps do not fail the backbone QA gate."""
    qa_slice = SummerLeagueSlice(2010, "14")
    raw_run = _raw_run(
        status=SummerLeagueRawRunStatus.PARTIAL,
        error_count=1,
        year=2010,
        league_id="14",
    )
    raw_files = [
        _raw_file(
            SummerLeagueRawFileStatus.MISSING,
            endpoint="playbyplayv2",
            game_id="1421000004",
            relative_path="2010/14/games/1421000004/playbyplayv2.json",
        )
    ]

    async def fake_raw_run(
        _db: object, slice_: SummerLeagueSlice
    ) -> SummerLeagueRawRun:
        return raw_run

    async def fake_raw_files(_db: object, raw_run_id: int) -> list[SummerLeagueRawFile]:
        return raw_files

    monkeypatch.setattr(service, "_load_raw_run", fake_raw_run)
    monkeypatch.setattr(service, "_load_raw_files", fake_raw_files)

    findings = await service.validate_raw_audit_integrity(object(), slice_=qa_slice)  # type: ignore[arg-type]

    severities = {finding.code: finding.severity for finding in findings}
    assert severities["RAW_RUN_ERRORS_RECORDED"] == SummerLeagueQASeverity.WARNING
    assert severities["RAW_FILE_MISSING"] == SummerLeagueQASeverity.WARNING


@pytest.mark.asyncio
async def test_normalization_parity_reports_count_and_link_mismatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Normalization parity compares raw counts to normalized rows."""
    qa_slice = SummerLeagueSlice(2024, "15")

    async def fake_raw_run(
        _db: object, slice_: SummerLeagueSlice
    ) -> SummerLeagueRawRun:
        return _raw_run(id=10, game_count=2, team_gamelog_rows=4, player_gamelog_rows=5)

    async def fake_competition(
        _db: object, slice_: SummerLeagueSlice
    ) -> SummerLeagueCompetition:
        return _competition(raw_run_id=99)

    async def fake_games(_db: object, competition_id: int) -> int:
        return 1

    async def fake_team_logs(_db: object, competition_id: int) -> int:
        return 3

    async def fake_player_logs(_db: object, competition_id: int) -> int:
        return 4

    monkeypatch.setattr(service, "_load_raw_run", fake_raw_run)
    monkeypatch.setattr(service, "_load_competition", fake_competition)
    monkeypatch.setattr(service, "_count_games", fake_games)
    monkeypatch.setattr(service, "_count_team_game_logs", fake_team_logs)
    monkeypatch.setattr(service, "_count_player_game_logs", fake_player_logs)

    findings = await service.validate_normalization_parity(object(), slice_=qa_slice)  # type: ignore[arg-type]

    assert [finding.code for finding in findings] == [
        "NORMALIZATION_RAW_RUN_LINK_MISMATCH",
        "NORMALIZATION_GAME_COUNT_MISMATCH",
        "NORMALIZATION_TEAM_LOG_COUNT_MISMATCH",
        "NORMALIZATION_PLAYER_LOG_COUNT_MISMATCH",
    ]


@pytest.mark.asyncio
async def test_player_resolution_reports_inconsistent_source_and_log_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Player-resolution validation checks source state against log links."""
    qa_slice = SummerLeagueSlice(2024, "15")
    rows = [
        (
            _source_player(canonical_player_id=None),
            _player_log(player_id=1),
        ),
        (
            _source_player(
                id=41,
                canonical_player_id=2,
                resolution_status=SummerLeagueResolutionStatus.UNRESOLVED,
            ),
            _player_log(id=51, source_player_id=41, player_id=None),
        ),
        (
            _source_player(
                id=42,
                canonical_player_id=3,
                resolution_status=SummerLeagueResolutionStatus.VECTOR_CANDIDATE,
                resolution_candidates=None,
            ),
            _player_log(id=52, source_player_id=42, player_id=4),
        ),
    ]

    async def fake_competition(
        _db: object, slice_: SummerLeagueSlice
    ) -> SummerLeagueCompetition:
        return _competition()

    async def fake_rows(
        _db: object, competition_id: int
    ) -> list[tuple[SummerLeagueSourcePlayer, SummerLeaguePlayerGameLog]]:
        return rows

    monkeypatch.setattr(service, "_load_competition", fake_competition)
    monkeypatch.setattr(service, "_load_player_log_resolution_rows", fake_rows)

    findings = await service.validate_player_resolution(object(), slice_=qa_slice)  # type: ignore[arg-type]

    assert set(finding.code for finding in findings) >= {
        "PLAYER_UNRESOLVED",
        "PLAYER_LOG_CANONICAL_LINK_MISMATCH",
        "PLAYER_RESOLUTION_INCONSISTENT",
        "PLAYER_LOG_CANONICAL_LINK_MISSING",
        "PLAYER_VECTOR_CANDIDATE_MISSING_EVIDENCE",
    }


@pytest.mark.asyncio
async def test_historical_data_quality_reports_flag_and_year_findings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Historical validation checks quality flags and source-player year ranges."""
    qa_slice = SummerLeagueSlice(2024, "15")

    async def fake_competition(
        _db: object, slice_: SummerLeagueSlice
    ) -> SummerLeagueCompetition:
        return _competition(pbp_available=False, shotchart_available=False)

    async def fake_sources(
        _db: object, competition_id: int
    ) -> list[SummerLeagueSourcePlayer]:
        return [
            _source_player(first_seen_year=2025, last_seen_year=2024),
            _source_player(id=41, first_seen_year=2020, last_seen_year=2023),
        ]

    monkeypatch.setattr(service, "_load_competition", fake_competition)
    monkeypatch.setattr(service, "_load_source_players_for_competition", fake_sources)

    findings = await service.validate_historical_data_quality(
        object(),  # type: ignore[arg-type]
        slices=[qa_slice],
    )

    assert set(finding.code for finding in findings) == {
        "DATA_QUALITY_FLAG_MISMATCH",
        "HISTORICAL_SOURCE_PLAYER_YEAR_RANGE_INVALID",
        "HISTORICAL_SOURCE_PLAYER_SLICE_OUT_OF_RANGE",
    }


@pytest.mark.asyncio
async def test_referential_integrity_reports_game_and_log_mismatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Referential validation reports missing teams and delegated log findings."""
    qa_slice = SummerLeagueSlice(2024, "15")
    delegated = [
        SummerLeagueQAFinding(
            severity=SummerLeagueQASeverity.ERROR,
            code="REFERENTIAL_PLAYER_LOG_SOURCE_MISMATCH",
            message="Mismatched source.",
        )
    ]

    async def fake_competition(
        _db: object, slice_: SummerLeagueSlice
    ) -> SummerLeagueCompetition:
        return _competition(raw_run_id=None)

    async def fake_games(_db: object, competition_id: int) -> list[SummerLeagueGame]:
        return [
            SummerLeagueGame(
                id=60,
                competition_id=30,
                nba_stats_game_id="1522400001",
                home_team_entry_id=None,
                away_team_entry_id=None,
            ),
            SummerLeagueGame(
                id=61,
                competition_id=30,
                nba_stats_game_id="1522400002",
                home_team_entry_id=1,
                away_team_entry_id=2,
            ),
        ]

    async def fake_teams(
        _db: object, competition_id: int
    ) -> dict[int, SummerLeagueTeamEntry]:
        return {
            1: SummerLeagueTeamEntry(
                id=1,
                competition_id=30,
                nba_stats_team_id="1",
                raw_team_name="A",
                team_slug="a",
            )
        }

    async def fake_team_logs(
        _db: object, competition_id: int
    ) -> list[SummerLeagueQAFinding]:
        return []

    async def fake_player_logs(
        _db: object, competition_id: int
    ) -> list[SummerLeagueQAFinding]:
        return delegated

    monkeypatch.setattr(service, "_load_competition", fake_competition)
    monkeypatch.setattr(service, "_load_games", fake_games)
    monkeypatch.setattr(service, "_load_teams_by_id", fake_teams)
    monkeypatch.setattr(service, "_validate_team_log_references", fake_team_logs)
    monkeypatch.setattr(service, "_validate_player_log_references", fake_player_logs)

    findings = await service.validate_referential_integrity(object(), slice_=qa_slice)  # type: ignore[arg-type]

    assert set(finding.code for finding in findings) == {
        "REFERENTIAL_COMPETITION_RAW_RUN_MISSING",
        "REFERENTIAL_GAME_TEAM_MISSING",
        "REFERENTIAL_GAME_TEAM_COMPETITION_MISMATCH",
        "REFERENTIAL_PLAYER_LOG_SOURCE_MISMATCH",
    }


@pytest.mark.asyncio
async def test_validators_handle_missing_slice_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validators return focused findings or no-op when slice rows are absent."""
    qa_slice = SummerLeagueSlice(2024, "15")

    async def no_raw_run(_db: object, slice_: SummerLeagueSlice) -> None:
        return None

    async def no_competition(_db: object, slice_: SummerLeagueSlice) -> None:
        return None

    monkeypatch.setattr(service, "_load_raw_run", no_raw_run)
    monkeypatch.setattr(service, "_load_competition", no_competition)

    raw_findings = await service.validate_raw_audit_integrity(
        object(),  # type: ignore[arg-type]
        slice_=qa_slice,
    )

    assert [finding.code for finding in raw_findings] == ["RAW_RUN_MISSING"]
    assert (
        await service.validate_normalization_parity(
            object(),  # type: ignore[arg-type]
            slice_=qa_slice,
        )
        == []
    )
    assert (
        await service.validate_player_resolution(
            object(),  # type: ignore[arg-type]
            slice_=qa_slice,
        )
        == []
    )
    assert (
        await service.validate_historical_data_quality(
            object(),  # type: ignore[arg-type]
            slices=[qa_slice],
        )
        == []
    )
    assert (
        await service.validate_referential_integrity(
            object(),  # type: ignore[arg-type]
            slice_=qa_slice,
        )
        == []
    )


@pytest.mark.asyncio
async def test_snapshot_and_qa_runner_delegate_to_count_and_validator_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Snapshot capture and aggregate QA runner compose helper functions."""
    qa_slice = SummerLeagueSlice(2024, "15")
    finding = SummerLeagueQAFinding(
        severity=SummerLeagueQASeverity.ERROR,
        code="RAW_RUN_MISSING",
        message="No raw run.",
    )

    async def count_raw_runs(_db: object, slices: list[SummerLeagueSlice]) -> int:
        return 1

    async def count_raw_files(_db: object, slices: list[SummerLeagueSlice]) -> int:
        return 2

    async def count_competitions(_db: object, slices: list[SummerLeagueSlice]) -> int:
        return 3

    async def count_scoped(
        _db: object, table: object, slices: list[SummerLeagueSlice]
    ) -> int:
        return 4

    async def count_sources(_db: object, slices: list[SummerLeagueSlice]) -> int:
        return 5

    async def raw_validator(
        _db: object, *, slice_: SummerLeagueSlice
    ) -> list[SummerLeagueQAFinding]:
        return [finding]

    async def empty_slice_validator(
        _db: object, *, slice_: SummerLeagueSlice
    ) -> list[SummerLeagueQAFinding]:
        return []

    async def empty_slices_validator(
        _db: object, *, slices: list[SummerLeagueSlice]
    ) -> list[SummerLeagueQAFinding]:
        return []

    monkeypatch.setattr(service, "_count_raw_runs", count_raw_runs)
    monkeypatch.setattr(service, "_count_raw_files", count_raw_files)
    monkeypatch.setattr(service, "_count_competitions", count_competitions)
    monkeypatch.setattr(service, "_count_scoped_table", count_scoped)
    monkeypatch.setattr(service, "_count_source_players", count_sources)
    monkeypatch.setattr(service, "validate_raw_audit_integrity", raw_validator)
    monkeypatch.setattr(service, "validate_normalization_parity", empty_slice_validator)
    monkeypatch.setattr(service, "validate_player_resolution", empty_slice_validator)
    monkeypatch.setattr(
        service, "validate_referential_integrity", empty_slice_validator
    )
    monkeypatch.setattr(
        service, "validate_historical_data_quality", empty_slices_validator
    )

    snapshot = await service.capture_summer_league_snapshot(
        object(),  # type: ignore[arg-type]
        slices=[qa_slice],
    )
    report = await service.run_summer_league_backbone_qa(
        object(),  # type: ignore[arg-type]
        slices=[qa_slice],
    )

    assert snapshot.table_counts == {
        "raw_runs": 1,
        "raw_files": 2,
        "competitions": 3,
        "team_entries": 4,
        "games": 4,
        "team_game_logs": 4,
        "source_players": 5,
        "player_game_logs": 4,
    }
    assert report.findings == [finding]


@pytest.mark.asyncio
async def test_query_helper_wrappers_read_fake_result_shapes() -> None:
    """Private query helpers consume scalar, scalars, and row result shapes."""
    team = SummerLeagueTeamEntry(
        id=70,
        competition_id=30,
        nba_stats_team_id="1610612753",
        raw_team_name="Magic",
        team_slug="magic",
    )
    source = _source_player()
    player_log = _player_log()
    db = _FakeDb(
        [
            _FakeResult(scalar=_raw_run()),
            _FakeResult(scalar=_competition()),
            _FakeResult(scalars=[_raw_file(SummerLeagueRawFileStatus.PARSED)]),
            _FakeResult(
                scalars=[
                    SummerLeagueGame(id=60, competition_id=30, nba_stats_game_id="1")
                ]
            ),
            _FakeResult(scalars=[team]),
            _FakeResult(rows=[(source, player_log)]),
            _FakeResult(scalars=[source]),
            _FakeResult(scalar=7),
        ]
    )

    assert await service._load_raw_run(db, SummerLeagueSlice(2024, "15")) is not None  # type: ignore[arg-type]
    assert (
        await service._load_competition(db, SummerLeagueSlice(2024, "15")) is not None
    )  # type: ignore[arg-type]
    assert len(await service._load_raw_files(db, 10)) == 1  # type: ignore[arg-type]
    assert len(await service._load_games(db, 30)) == 1  # type: ignore[arg-type]
    assert await service._load_teams_by_id(db, 30) == {70: team}  # type: ignore[arg-type]
    assert await service._load_player_log_resolution_rows(db, 30) == [
        (source, player_log)
    ]  # type: ignore[arg-type]
    assert await service._load_source_players_for_competition(db, 30) == [source]  # type: ignore[arg-type]
    assert await service._scalar_count(db, object()) == 7  # type: ignore[arg-type]
