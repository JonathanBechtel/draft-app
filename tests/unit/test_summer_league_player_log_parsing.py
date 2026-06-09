"""Unit tests for Summer League player game-log parsing."""

from __future__ import annotations

import json
from pathlib import Path

from app.services.summer_league.normalization import (
    parse_minutes_to_seconds,
    parse_player_box_rows,
    parse_player_gamelog,
)


def _result_set(
    name: str, headers: list[str], rows: list[list[object]]
) -> dict[str, object]:
    return {"name": name, "headers": headers, "rowSet": rows}


def test_parse_minutes_to_seconds_handles_common_nba_formats() -> None:
    """Minute parsing converts clock strings and numeric minutes to seconds."""
    assert parse_minutes_to_seconds("24:34") == 1474
    assert parse_minutes_to_seconds("12") == 720
    assert parse_minutes_to_seconds(8.5) == 510
    assert parse_minutes_to_seconds("") is None


def test_parse_player_gamelog_preserves_source_identity(tmp_path: Path) -> None:
    """Season player gamelog parsing extracts NBA.com person IDs and names."""
    path = tmp_path / "leaguegamelog_player.json"
    path.write_text(
        json.dumps(
            {
                "resultSets": [
                    _result_set(
                        "LeagueGameLog",
                        ["PLAYER_ID", "PLAYER_NAME", "TEAM_ID"],
                        [[1640001, "Test Prospect Jr.", 1610612753]],
                    )
                ]
            }
        )
    )

    rows = parse_player_gamelog(path)

    assert rows[0].nba_stats_person_id == "1640001"
    assert rows[0].raw_player_name == "Test Prospect Jr."
    assert rows[0].nba_stats_team_id == "1610612753"


def test_parse_player_box_rows_merges_traditional_advanced_and_scoring(
    tmp_path: Path,
) -> None:
    """Player box parsing merges stat endpoints by game, person, and team."""
    game_dir = tmp_path / "games" / "1522400001"
    game_dir.mkdir(parents=True)
    game_dir.joinpath("boxscoretraditionalv2.json").write_text(
        json.dumps(
            {
                "resultSets": [
                    _result_set(
                        "PlayerStats",
                        [
                            "GAME_ID",
                            "TEAM_ID",
                            "PLAYER_ID",
                            "PLAYER_NAME",
                            "START_POSITION",
                            "COMMENT",
                            "MIN",
                            "FGM",
                            "FGA",
                            "FG_PCT",
                            "PTS",
                            "PLUS_MINUS",
                        ],
                        [
                            [
                                "1522400001",
                                1610612753,
                                1640001,
                                "Test Prospect",
                                "F",
                                "",
                                "24:34",
                                7,
                                12,
                                0.583,
                                18,
                                9,
                            ],
                            [
                                "1522400001",
                                1610612753,
                                1640002,
                                "Bench Dnp",
                                "",
                                "DNP - Coach's Decision",
                                "",
                                "",
                                "",
                                "",
                                "",
                                "",
                            ],
                        ],
                    )
                ]
            }
        )
    )
    game_dir.joinpath("boxscoreadvancedv2.json").write_text(
        json.dumps(
            {
                "resultSets": [
                    _result_set(
                        "PlayerStats",
                        [
                            "GAME_ID",
                            "TEAM_ID",
                            "PLAYER_ID",
                            "PLAYER_NAME",
                            "OFF_RATING",
                            "USG_PCT",
                            "PIE",
                        ],
                        [
                            [
                                "1522400001",
                                1610612753,
                                1640001,
                                "Test Prospect",
                                118.4,
                                0.231,
                                0.134,
                            ]
                        ],
                    )
                ]
            }
        )
    )
    game_dir.joinpath("boxscorescoringv2.json").write_text(
        json.dumps(
            {
                "resultSets": [
                    _result_set(
                        "sqlPlayersScoring",
                        [
                            "GAME_ID",
                            "TEAM_ID",
                            "PLAYER_ID",
                            "PLAYER_NAME",
                            "PCT_FGA_2PT",
                            "PCT_PTS_3PT",
                        ],
                        [
                            [
                                "1522400001",
                                1610612753,
                                1640001,
                                "Test Prospect",
                                0.667,
                                0.333,
                            ]
                        ],
                    )
                ]
            }
        )
    )

    rows = parse_player_box_rows(tmp_path)

    prospect = next(row for row in rows if row.nba_stats_person_id == "1640001")
    dnp = next(row for row in rows if row.nba_stats_person_id == "1640002")
    assert prospect.minutes_seconds == 1474
    assert prospect.pts == 18
    assert prospect.off_rating == 118.4
    assert prospect.usg_pct == 0.231
    assert prospect.pct_fga_2pt == 0.667
    assert prospect.pct_pts_3pt == 0.333
    assert dnp.comment == "DNP - Coach's Decision"
    assert dnp.minutes_seconds is None
    assert dnp.pts is None
