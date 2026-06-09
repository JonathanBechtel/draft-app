"""Unit tests for Summer League QA report DTOs and snapshot helpers."""

from __future__ import annotations

from app.services.summer_league.qa import (
    SummerLeagueQAFinding,
    SummerLeagueQAReport,
    SummerLeagueQASnapshot,
    SummerLeagueQASeverity,
    SummerLeagueSlice,
    compare_idempotency_snapshots,
)


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
