"""Unit checks for Summer League schema enum and table contracts."""

from __future__ import annotations

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


def test_product_enum_values_match_spec() -> None:
    """Product enum values stay aligned with the detailed schema contract."""
    assert [quality.value for quality in SummerLeagueDataQuality] == [
        "full",
        "partial",
        "box_only",
        "raw_only",
    ]
    assert [status.value for status in SummerLeagueGameStatus] == [
        "scheduled",
        "final",
        "unknown",
    ]
    assert [status.value for status in SummerLeagueResolutionStatus] == [
        "UNRESOLVED",
        "EXTERNAL_ID",
        "EXACT",
        "ALIAS",
        "FUZZY",
        "VECTOR_CANDIDATE",
        "MANUAL",
        "STUB",
    ]


def test_product_table_names_are_grouped_under_summer_league_prefix() -> None:
    """Normalized product tables use explicit Summer League table names."""
    assert SummerLeagueCompetition.__table__.name == "summer_league_competitions"  # type: ignore[attr-defined]
    assert SummerLeagueTeamEntry.__table__.name == "summer_league_team_entries"  # type: ignore[attr-defined]
    assert SummerLeagueGame.__table__.name == "summer_league_games"  # type: ignore[attr-defined]
    assert SummerLeagueSourcePlayer.__table__.name == "summer_league_source_players"  # type: ignore[attr-defined]
    assert SummerLeagueTeamGameLog.__table__.name == "summer_league_team_game_logs"  # type: ignore[attr-defined]
    assert SummerLeaguePlayerGameLog.__table__.name == "summer_league_player_game_logs"  # type: ignore[attr-defined]


def test_product_table_uniqueness_constraints_are_named() -> None:
    """Product tables expose stable uniqueness constraints for upsert services."""
    tables = [
        SummerLeagueCompetition.__table__,  # type: ignore[attr-defined]
        SummerLeagueTeamEntry.__table__,  # type: ignore[attr-defined]
        SummerLeagueGame.__table__,  # type: ignore[attr-defined]
        SummerLeagueSourcePlayer.__table__,  # type: ignore[attr-defined]
        SummerLeagueTeamGameLog.__table__,  # type: ignore[attr-defined]
        SummerLeaguePlayerGameLog.__table__,  # type: ignore[attr-defined]
    ]
    constraint_names = {
        constraint.name
        for table in tables
        for constraint in table.constraints
        if constraint.name is not None
    }

    assert constraint_names >= {
        "uq_summer_league_competitions_year_league",
        "uq_summer_league_team_entries_competition_source_team",
        "uq_summer_league_games_nba_stats_game_id",
        "uq_summer_league_source_players_nba_stats_person_id",
        "uq_summer_league_team_game_logs_game_team",
        "uq_summer_league_player_game_logs_game_person_team",
    }
