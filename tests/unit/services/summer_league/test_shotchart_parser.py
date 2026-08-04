"""Unit tests for the shotchartdetail shot-event parser.

Tests verify field mapping from a trimmed fixture payload, the made/missed
flag, and graceful handling of missing or empty files and result sets.
No database required.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.sources.summer_league.normalization import (
    ParsedShotEvent,
    parse_shot_rows,
)

FIXTURE_PATH = (
    Path(__file__).parent.parent.parent.parent
    / "fixtures"
    / "summer_league"
    / "shotchartdetail_sample.json"
)

HEADERS = [
    "GRID_TYPE",
    "GAME_ID",
    "GAME_EVENT_ID",
    "PLAYER_ID",
    "PLAYER_NAME",
    "TEAM_ID",
    "TEAM_NAME",
    "PERIOD",
    "MINUTES_REMAINING",
    "SECONDS_REMAINING",
    "EVENT_TYPE",
    "ACTION_TYPE",
    "SHOT_TYPE",
    "SHOT_ZONE_BASIC",
    "SHOT_ZONE_AREA",
    "SHOT_ZONE_RANGE",
    "SHOT_DISTANCE",
    "LOC_X",
    "LOC_Y",
    "SHOT_ATTEMPTED_FLAG",
    "SHOT_MADE_FLAG",
    "GAME_DATE",
    "HTM",
    "VTM",
]


def _make_row(
    *,
    game_id: str = "1522400001",
    event_id: int = 42,
    player_id: int = 1640001,
    player_name: str = "Test Player A",
    team_id: int = 1610612753,
    team_name: str = "Orlando Magic",
    period: int = 1,
    minutes_remaining: int = 9,
    seconds_remaining: int = 30,
    event_type: str = "Made Shot",
    action_type: str = "Jump Shot",
    shot_type: str = "3PT Field Goal",
    shot_zone_basic: str = "Above the Break 3",
    shot_zone_area: str = "Left Side Center(LC)",
    shot_zone_range: str = "24+ ft.",
    shot_distance: int = 25,
    loc_x: int = -120,
    loc_y: int = 60,
    shot_attempted_flag: int = 1,
    shot_made_flag: int = 1,
    game_date: str = "2024-07-12",
    htm: str = "ORL",
    vtm: str = "CLE",
) -> list[object]:
    return [
        "Shot Chart Detail",
        game_id,
        event_id,
        player_id,
        player_name,
        team_id,
        team_name,
        period,
        minutes_remaining,
        seconds_remaining,
        event_type,
        action_type,
        shot_type,
        shot_zone_basic,
        shot_zone_area,
        shot_zone_range,
        shot_distance,
        loc_x,
        loc_y,
        shot_attempted_flag,
        shot_made_flag,
        game_date,
        htm,
        vtm,
    ]


def _write_shotchart(tmp_path: Path, rows: list[list[object]]) -> Path:
    path = tmp_path / "shotchartdetail.json"
    path.write_text(
        json.dumps(
            {
                "resultSets": [
                    {"name": "Shot_Chart_Detail", "headers": HEADERS, "rowSet": rows}
                ]
            }
        )
    )
    return path


def test_parse_shot_rows_from_fixture() -> None:
    """Fixture file parses into exactly two rows with the correct game ID."""
    rows = parse_shot_rows(FIXTURE_PATH)
    assert len(rows) == 2
    assert all(r.nba_stats_game_id == "1522400001" for r in rows)


def test_parse_shot_rows_field_mapping(tmp_path: Path) -> None:
    """All fields map from the JSON row to ParsedShotEvent correctly."""
    path = _write_shotchart(tmp_path, [_make_row()])
    rows = parse_shot_rows(path)
    assert len(rows) == 1
    row = rows[0]
    assert isinstance(row, ParsedShotEvent)
    assert row.nba_stats_game_id == "1522400001"
    assert row.nba_stats_game_event_id == 42
    assert row.nba_stats_person_id == "1640001"
    assert row.raw_player_name == "Test Player A"
    assert row.nba_stats_team_id == "1610612753"
    assert row.period == 1
    assert row.minutes_remaining == 9
    assert row.seconds_remaining == 30
    assert row.loc_x == -120
    assert row.loc_y == 60
    assert row.shot_distance == 25
    assert row.shot_type == "3PT Field Goal"
    assert row.shot_zone_basic == "Above the Break 3"
    assert row.shot_zone_area == "Left Side Center(LC)"
    assert row.shot_zone_range == "24+ ft."
    assert row.action_type == "Jump Shot"
    assert row.made is True


def test_parse_shot_rows_made_flag_false(tmp_path: Path) -> None:
    """SHOT_MADE_FLAG=0 maps to made=False."""
    path = _write_shotchart(tmp_path, [_make_row(shot_made_flag=0)])
    rows = parse_shot_rows(path)
    assert len(rows) == 1
    assert rows[0].made is False


def test_parse_shot_rows_made_flag_true(tmp_path: Path) -> None:
    """SHOT_MADE_FLAG=1 maps to made=True."""
    path = _write_shotchart(tmp_path, [_make_row(shot_made_flag=1)])
    rows = parse_shot_rows(path)
    assert rows[0].made is True


def test_parse_shot_rows_missing_file(tmp_path: Path) -> None:
    """Returns an empty list when the file does not exist."""
    rows = parse_shot_rows(tmp_path / "nonexistent.json")
    assert rows == []


def test_parse_shot_rows_empty_result_set(tmp_path: Path) -> None:
    """Returns an empty list when Shot_Chart_Detail has no rows."""
    path = _write_shotchart(tmp_path, [])
    rows = parse_shot_rows(path)
    assert rows == []


def test_parse_shot_rows_wrong_result_set_name(tmp_path: Path) -> None:
    """Returns an empty list when no Shot_Chart_Detail result set is present."""
    path = tmp_path / "shotchartdetail.json"
    path.write_text(
        json.dumps(
            {
                "resultSets": [
                    {
                        "name": "LeagueAverages",
                        "headers": HEADERS,
                        "rowSet": [_make_row()],
                    }
                ]
            }
        )
    )
    rows = parse_shot_rows(path)
    assert rows == []


def test_parse_shot_rows_skips_row_missing_game_id(tmp_path: Path) -> None:
    """Rows missing GAME_ID are skipped."""
    bad_headers = [h for h in HEADERS if h != "GAME_ID"]
    path = tmp_path / "shotchartdetail.json"
    path.write_text(
        json.dumps(
            {
                "resultSets": [
                    {
                        "name": "Shot_Chart_Detail",
                        "headers": bad_headers,
                        "rowSet": [
                            _make_row()[1:]
                        ],  # drop first col (GRID_TYPE was prepended)
                    }
                ]
            }
        )
    )
    # Row has no GAME_ID key so should be dropped
    rows = parse_shot_rows(path)
    assert rows == []


def test_parse_shot_rows_2pt_shot_type(tmp_path: Path) -> None:
    """2PT Field Goal shot type is stored as-is."""
    path = _write_shotchart(tmp_path, [_make_row(shot_type="2PT Field Goal")])
    rows = parse_shot_rows(path)
    assert rows[0].shot_type == "2PT Field Goal"


def test_parse_shot_rows_null_optional_fields(tmp_path: Path) -> None:
    """Optional numeric fields accept None without error."""
    path = tmp_path / "shotchartdetail.json"
    sparse_headers = [
        "GAME_ID",
        "GAME_EVENT_ID",
        "PLAYER_ID",
        "PLAYER_NAME",
        "TEAM_ID",
        "SHOT_MADE_FLAG",
    ]
    path.write_text(
        json.dumps(
            {
                "resultSets": [
                    {
                        "name": "Shot_Chart_Detail",
                        "headers": sparse_headers,
                        "rowSet": [
                            ["1522400001", 99, 1640001, "Sparse Player", 1610612753, 0]
                        ],
                    }
                ]
            }
        )
    )
    rows = parse_shot_rows(path)
    assert len(rows) == 1
    assert rows[0].loc_x is None
    assert rows[0].loc_y is None
    assert rows[0].period is None
    assert rows[0].shot_type is None
