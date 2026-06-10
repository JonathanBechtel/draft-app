"""Sanity checks for compact Summer League QA fixture trees."""

from __future__ import annotations

import json
from pathlib import Path

from app.schemas.summer_league import (
    SummerLeagueRawFileStatus,
    SummerLeagueRawRunStatus,
)
from app.services.summer_league.audit import audit_raw_run, discover_manifest_paths

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "summer_league"


def _audit_fixture(name: str, year: int, league_id: str):
    manifest_path = FIXTURE_ROOT / name / str(year) / league_id / "manifest.json"
    return audit_raw_run(raw_root=FIXTURE_ROOT / name, manifest_path=manifest_path)


def test_fixture_manifest_set_covers_expected_scenario_types() -> None:
    """Fixture roots cover modern, satellite, partial, corrupt, and missing cases."""
    discovered = {
        path.relative_to(FIXTURE_ROOT / scenario).as_posix()
        for scenario in (
            "modern",
            "satellite",
            "partial",
            "corrupt",
            "missing_endpoint",
        )
        for path in discover_manifest_paths(raw_root=FIXTURE_ROOT / scenario)
    }

    assert discovered == {
        "2024/15/manifest.json",
        "2024/13/manifest.json",
        "2010/14/manifest.json",
        "2007/15/manifest.json",
    }


def test_modern_and_satellite_fixtures_are_complete_runs() -> None:
    """Modern Las Vegas and satellite fixtures include every expected endpoint."""
    modern = _audit_fixture("modern", 2024, "15")
    satellite = _audit_fixture("satellite", 2024, "13")

    assert modern.status == SummerLeagueRawRunStatus.COMPLETE
    assert satellite.status == SummerLeagueRawRunStatus.COMPLETE
    assert len(modern.files) == 8
    assert len(satellite.files) == 8


def test_partial_corrupt_and_missing_endpoint_fixtures_emit_bad_statuses() -> None:
    """Negative fixture trees expose partial, corrupt, and missing raw files."""
    partial = _audit_fixture("partial", 2010, "14")
    corrupt = _audit_fixture("corrupt", 2024, "15")
    missing = _audit_fixture("missing_endpoint", 2007, "15")

    assert partial.status == SummerLeagueRawRunStatus.PARTIAL
    assert corrupt.status == SummerLeagueRawRunStatus.FAILED
    assert missing.status == SummerLeagueRawRunStatus.PARTIAL
    assert {
        file.descriptor.endpoint
        for file in partial.files
        if file.parse_status == SummerLeagueRawFileStatus.MISSING
    } == {"playbyplayv2", "shotchartdetail"}
    assert {
        file.descriptor.endpoint
        for file in corrupt.files
        if file.parse_status == SummerLeagueRawFileStatus.PARSE_FAILED
    } == {"boxscoretraditionalv2"}
    assert {
        file.descriptor.endpoint
        for file in missing.files
        if file.parse_status == SummerLeagueRawFileStatus.MISSING
    } == {"shotchartdetail"}


def test_db_negative_case_fixture_has_seed_values() -> None:
    """DB fixture metadata names the seed values used by negative tests."""
    payload = json.loads((FIXTURE_ROOT / "db_negative_cases.json").read_text())

    assert payload["duplicate_raw_files"]["endpoint"] == "leaguegamelog_player"
    assert len(payload["duplicate_raw_files"]["relative_paths"]) == 2
    assert payload["orphan_competition"]["league_id"] == "13"
    assert payload["unresolved_player"]["nba_stats_person_id"] == "1649999"
