"""Unit checks for Summer League schema enum and table contracts."""

from __future__ import annotations

from app.schemas.summer_league import (
    SummerLeagueRawFile,
    SummerLeagueRawFileStatus,
    SummerLeagueRawRun,
    SummerLeagueRawRunStatus,
)


def test_raw_audit_enum_values_match_spec() -> None:
    """Raw audit enum values stay aligned with the project schema contract."""
    assert [status.value for status in SummerLeagueRawRunStatus] == [
        "PENDING",
        "COMPLETE",
        "PARTIAL",
        "FAILED",
    ]
    assert [status.value for status in SummerLeagueRawFileStatus] == [
        "PRESENT",
        "MISSING",
        "EMPTY",
        "PARSED",
        "PARSE_FAILED",
        "SKIPPED",
    ]


def test_raw_run_table_contract_names_constraints_and_indexes() -> None:
    """Raw run table exposes the expected table name, constraints, and indexes."""
    table = SummerLeagueRawRun.__table__  # type: ignore[attr-defined]

    assert table.name == "summer_league_raw_runs"
    assert {
        constraint.name
        for constraint in table.constraints
        if constraint.name is not None
    } >= {
        "uq_summer_league_raw_runs_year_league_manifest",
        "ck_summer_league_raw_runs_year",
    }
    assert {index.name for index in table.indexes} >= {
        "ix_summer_league_raw_runs_year_league",
        "ix_summer_league_raw_runs_status",
    }
    assert table.c.status.type.name == "summer_league_raw_run_status_enum"


def test_raw_file_table_contract_names_constraints_and_indexes() -> None:
    """Raw file table exposes expected constraints, indexes, and enum type."""
    table = SummerLeagueRawFile.__table__  # type: ignore[attr-defined]

    assert table.name == "summer_league_raw_files"
    assert {
        constraint.name
        for constraint in table.constraints
        if constraint.name is not None
    } >= {
        "uq_summer_league_raw_files_run_endpoint_game",
        "uq_summer_league_raw_files_relative_path",
    }
    assert {index.name for index in table.indexes} >= {
        "ix_summer_league_raw_files_year_league_endpoint",
        "ix_summer_league_raw_files_game_id",
        "ix_summer_league_raw_files_parse_status",
    }
    assert table.c.parse_status.type.name == "summer_league_raw_file_status_enum"
