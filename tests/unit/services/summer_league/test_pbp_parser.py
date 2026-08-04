"""Unit tests for the playbyplayv2 PBP-event parser.

Tests verify field mapping from a trimmed fixture payload, score/margin
parsing, actor ID handling, and graceful fallbacks for missing or empty
files and result sets.  No database required.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.sources.summer_league.normalization import (
    ParsedPBPEvent,
    parse_pbp_rows,
)

FIXTURE_PATH = (
    Path(__file__).parent.parent.parent.parent
    / "fixtures"
    / "summer_league"
    / "playbyplayv2_sample.json"
)

HEADERS = [
    "GAME_ID",
    "EVENTNUM",
    "EVENTMSGTYPE",
    "PERIOD",
    "WCTIMESTRING",
    "PCTIMESTRING",
    "HOMEDESCRIPTION",
    "NEUTRALDESCRIPTION",
    "VISITORDESCRIPTION",
    "SCORE",
    "SCOREMARGIN",
    "PERSON1TYPE",
    "PLAYER1_ID",
    "PLAYER1_NAME",
    "PLAYER1_TEAM_ID",
    "PLAYER1_TEAM_CITY",
    "PLAYER1_TEAM_NICKNAME",
    "PLAYER1_TEAM_ABBREVIATION",
    "PERSON2TYPE",
    "PLAYER2_ID",
    "PLAYER2_NAME",
    "PLAYER2_TEAM_ID",
    "PLAYER2_TEAM_CITY",
    "PLAYER2_TEAM_NICKNAME",
    "PLAYER2_TEAM_ABBREVIATION",
    "PERSON3TYPE",
    "PLAYER3_ID",
    "PLAYER3_NAME",
    "PLAYER3_TEAM_ID",
    "PLAYER3_TEAM_CITY",
    "PLAYER3_TEAM_NICKNAME",
    "PLAYER3_TEAM_ABBREVIATION",
    "VIDEO_AVAILABLE_FLAG",
]


def _make_row(
    *,
    game_id: str = "1522400001",
    event_num: int = 10,
    event_msg_type: int = 1,
    period: int = 1,
    pctimestring: str = "11:30",
    home_description: object = "Test Player A 3PT Jump Shot (3 PTS)",
    neutral_description: object = None,
    visitor_description: object = None,
    score: object = "3 - 0",
    score_margin: object = "3",
    player1_id: object = 1640001,
    player1_name: str = "Test Player A",
    player2_id: object = 0,
    player2_name: object = None,
    player3_id: object = 0,
    player3_name: object = None,
) -> list[object]:
    return [
        game_id,
        event_num,
        event_msg_type,
        period,
        "7:00 PM",
        pctimestring,
        home_description,
        neutral_description,
        visitor_description,
        score,
        score_margin,
        4,
        player1_id,
        player1_name,
        1610612753,
        "Orlando",
        "Magic",
        "ORL",
        0,
        player2_id,
        player2_name,
        0,
        None,
        None,
        None,
        0,
        player3_id,
        player3_name,
        0,
        None,
        None,
        None,
        1,
    ]


def _write_pbp(tmp_path: Path, rows: list[list[object]]) -> Path:
    path = tmp_path / "playbyplayv2.json"
    path.write_text(
        json.dumps(
            {"resultSets": [{"name": "PlayByPlay", "headers": HEADERS, "rowSet": rows}]}
        )
    )
    return path


def test_parse_pbp_rows_from_fixture() -> None:
    """Fixture file parses into exactly two rows with the correct game ID."""
    rows = parse_pbp_rows(FIXTURE_PATH)
    assert len(rows) == 2
    assert all(r.nba_stats_game_id == "1522400001" for r in rows)


def test_parse_pbp_rows_field_mapping(tmp_path: Path) -> None:
    """All core fields map from the JSON row to ParsedPBPEvent correctly."""
    path = _write_pbp(tmp_path, [_make_row()])
    rows = parse_pbp_rows(path)
    assert len(rows) == 1
    row = rows[0]
    assert isinstance(row, ParsedPBPEvent)
    assert row.nba_stats_game_id == "1522400001"
    assert row.event_num == 10
    assert row.event_msg_type == 1
    assert row.period == 1
    assert row.clock == "11:30"
    assert row.home_score == 3
    assert row.away_score == 0
    assert row.score_margin == 3
    assert row.person1_nba_id == "1640001"
    assert row.person2_nba_id is None
    assert row.person3_nba_id is None
    assert row.description == "Test Player A 3PT Jump Shot (3 PTS)"


def test_parse_pbp_rows_score_tie(tmp_path: Path) -> None:
    """SCOREMARGIN='TIE' maps to score_margin=0."""
    path = _write_pbp(tmp_path, [_make_row(score="5 - 5", score_margin="TIE")])
    rows = parse_pbp_rows(path)
    assert len(rows) == 1
    assert rows[0].score_margin == 0
    assert rows[0].home_score == 5
    assert rows[0].away_score == 5


def test_parse_pbp_rows_null_score(tmp_path: Path) -> None:
    """Null SCORE and SCOREMARGIN map to None on non-scoring events."""
    path = _write_pbp(tmp_path, [_make_row(score=None, score_margin=None)])
    rows = parse_pbp_rows(path)
    assert len(rows) == 1
    assert rows[0].home_score is None
    assert rows[0].away_score is None
    assert rows[0].score_margin is None


def test_parse_pbp_rows_negative_margin(tmp_path: Path) -> None:
    """Negative SCOREMARGIN (visitor lead) maps to a negative integer."""
    path = _write_pbp(tmp_path, [_make_row(score="0 - 3", score_margin="-3")])
    rows = parse_pbp_rows(path)
    assert rows[0].score_margin == -3
    assert rows[0].home_score == 0
    assert rows[0].away_score == 3


def test_parse_pbp_rows_person2_present(tmp_path: Path) -> None:
    """Person2 (assister) maps to person2_nba_id when non-zero."""
    path = _write_pbp(
        tmp_path,
        [
            _make_row(
                player2_id=1640003,
                player2_name="Test Player C",
            )
        ],
    )
    rows = parse_pbp_rows(path)
    assert rows[0].person2_nba_id == "1640003"


def test_parse_pbp_rows_person3_present(tmp_path: Path) -> None:
    """Person3 maps to person3_nba_id when non-zero."""
    path = _write_pbp(
        tmp_path,
        [_make_row(player3_id=1640004, player3_name="Test Player D")],
    )
    rows = parse_pbp_rows(path)
    assert rows[0].person3_nba_id == "1640004"


def test_parse_pbp_rows_zero_person_id_is_none(tmp_path: Path) -> None:
    """Person IDs of 0 map to None (no actor for that slot)."""
    path = _write_pbp(tmp_path, [_make_row(player2_id=0, player3_id=0)])
    rows = parse_pbp_rows(path)
    assert rows[0].person2_nba_id is None
    assert rows[0].person3_nba_id is None


def test_parse_pbp_rows_description_neutral(tmp_path: Path) -> None:
    """NEUTRALDESCRIPTION is included in the combined description."""
    path = _write_pbp(
        tmp_path,
        [
            _make_row(
                home_description=None,
                neutral_description="Jump Ball A vs. B",
                visitor_description=None,
                score=None,
                score_margin=None,
                player1_id=1640001,
                player2_id=1640002,
                player2_name="Test Player B",
            )
        ],
    )
    rows = parse_pbp_rows(path)
    assert rows[0].description == "Jump Ball A vs. B"


def test_parse_pbp_rows_description_visitor_only(tmp_path: Path) -> None:
    """VISITORDESCRIPTION alone produces a non-null description."""
    path = _write_pbp(
        tmp_path,
        [
            _make_row(
                home_description=None,
                neutral_description=None,
                visitor_description="MISS Test Player B 3PT Jump Shot",
                score=None,
                score_margin=None,
            )
        ],
    )
    rows = parse_pbp_rows(path)
    assert rows[0].description == "MISS Test Player B 3PT Jump Shot"


def test_parse_pbp_rows_all_descriptions_combined(tmp_path: Path) -> None:
    """Multiple non-null description columns are joined with a space."""
    path = _write_pbp(
        tmp_path,
        [
            _make_row(
                home_description="Home desc",
                neutral_description=None,
                visitor_description="Visitor desc",
            )
        ],
    )
    rows = parse_pbp_rows(path)
    assert rows[0].description == "Home desc Visitor desc"


def test_parse_pbp_rows_missing_file(tmp_path: Path) -> None:
    """Returns an empty list when the file does not exist."""
    rows = parse_pbp_rows(tmp_path / "nonexistent.json")
    assert rows == []


def test_parse_pbp_rows_empty_result_set(tmp_path: Path) -> None:
    """Returns an empty list when PlayByPlay has no rows."""
    path = _write_pbp(tmp_path, [])
    rows = parse_pbp_rows(path)
    assert rows == []


def test_parse_pbp_rows_wrong_result_set_name(tmp_path: Path) -> None:
    """Returns an empty list when no PlayByPlay result set is present."""
    path = tmp_path / "playbyplayv2.json"
    path.write_text(
        json.dumps(
            {
                "resultSets": [
                    {
                        "name": "PlayByPlayV3",
                        "headers": HEADERS,
                        "rowSet": [_make_row()],
                    }
                ]
            }
        )
    )
    rows = parse_pbp_rows(path)
    assert rows == []


def test_parse_pbp_rows_skips_row_missing_game_id(tmp_path: Path) -> None:
    """Rows missing GAME_ID are skipped."""
    headers_without_game_id = [h for h in HEADERS if h != "GAME_ID"]
    # Build a row without the GAME_ID slot
    full_row = _make_row()
    game_id_index = HEADERS.index("GAME_ID")
    short_row = full_row[:game_id_index] + full_row[game_id_index + 1 :]
    path = tmp_path / "playbyplayv2.json"
    path.write_text(
        json.dumps(
            {
                "resultSets": [
                    {
                        "name": "PlayByPlay",
                        "headers": headers_without_game_id,
                        "rowSet": [short_row],
                    }
                ]
            }
        )
    )
    rows = parse_pbp_rows(path)
    assert rows == []


def test_parse_pbp_rows_multiple_events(tmp_path: Path) -> None:
    """Multiple rows in one file all parse correctly."""
    path = _write_pbp(
        tmp_path,
        [
            _make_row(
                event_num=1,
                period=1,
                pctimestring="12:00",
                score=None,
                score_margin=None,
            ),
            _make_row(
                event_num=2,
                period=1,
                pctimestring="11:45",
                score="3 - 0",
                score_margin="3",
            ),
            _make_row(
                event_num=3,
                period=2,
                pctimestring="12:00",
                score="3 - 0",
                score_margin="3",
            ),
        ],
    )
    rows = parse_pbp_rows(path)
    assert len(rows) == 3
    assert [r.event_num for r in rows] == [1, 2, 3]
    assert rows[2].period == 2
