"""Unit tests for Summer League competition/team/game normalization helpers."""

from __future__ import annotations

import json
from pathlib import Path

from app.services.summer_league.normalization import (
    parse_minutes_to_int,
    parse_team_box_rows,
    parse_team_gamelog,
    team_slug,
)


def _result_set(
    name: str, headers: list[str], rows: list[list[object]]
) -> dict[str, object]:
    return {"name": name, "headers": headers, "rowSet": rows}


def test_parse_minutes_to_int_handles_nba_values() -> None:
    """Minute values parse from numeric and NBA clock strings."""
    assert parse_minutes_to_int("200:00") == 200
    assert parse_minutes_to_int("24:28") == 24
    assert parse_minutes_to_int(213) == 213
    assert parse_minutes_to_int("") is None


def test_team_slug_uses_source_name_or_abbreviation() -> None:
    """Team slugs are deterministic from source names."""
    assert team_slug("Los Angeles Lakers", "LAL") == "los-angeles-lakers"
    assert team_slug("", "NYK") == "nyk"


def test_parse_team_gamelog_extracts_source_team_and_game_fields(
    tmp_path: Path,
) -> None:
    """Team gamelog parser extracts stable IDs, dates, matchup, and points."""
    path = tmp_path / "leaguegamelog_team.json"
    path.write_text(
        json.dumps(
            {
                "resultSets": [
                    _result_set(
                        "LeagueGameLog",
                        [
                            "TEAM_ID",
                            "TEAM_ABBREVIATION",
                            "TEAM_NAME",
                            "GAME_ID",
                            "GAME_DATE",
                            "MATCHUP",
                            "PTS",
                        ],
                        [
                            [
                                1610612748,
                                "MIA",
                                "Miami Heat",
                                "1522400076",
                                "2024-07-22",
                                "MIA vs. MEM",
                                120,
                            ]
                        ],
                    )
                ]
            }
        )
    )

    rows = parse_team_gamelog(path)

    assert len(rows) == 1
    assert rows[0].game_id == "1522400076"
    assert rows[0].nba_stats_team_id == "1610612748"
    assert rows[0].game_date is not None
    assert rows[0].pts == 120


def test_parse_team_box_rows_merges_traditional_and_advanced(tmp_path: Path) -> None:
    """Team box parser merges traditional and advanced TeamStats rows."""
    game_dir = tmp_path / "games" / "1522400001"
    game_dir.mkdir(parents=True)
    game_dir.joinpath("boxscoretraditionalv2.json").write_text(
        json.dumps(
            {
                "resultSets": [
                    _result_set("PlayerStats", [], []),
                    _result_set(
                        "TeamStats",
                        [
                            "GAME_ID",
                            "TEAM_ID",
                            "TEAM_NAME",
                            "TEAM_ABBREVIATION",
                            "MIN",
                            "FGM",
                            "FGA",
                            "PTS",
                            "PLUS_MINUS",
                        ],
                        [
                            [
                                "1522400001",
                                1610612753,
                                "Magic",
                                "ORL",
                                "200:00",
                                36,
                                76,
                                106,
                                4,
                            ]
                        ],
                    ),
                ]
            }
        )
    )
    game_dir.joinpath("boxscoreadvancedv2.json").write_text(
        json.dumps(
            {
                "resultSets": [
                    _result_set("PlayerStats", [], []),
                    _result_set(
                        "TeamStats",
                        [
                            "GAME_ID",
                            "TEAM_ID",
                            "TEAM_NAME",
                            "TEAM_ABBREVIATION",
                            "OFF_RATING",
                            "DEF_RATING",
                            "PACE",
                        ],
                        [
                            [
                                "1522400001",
                                1610612753,
                                "Magic",
                                "ORL",
                                115.2,
                                85.9,
                                110.84,
                            ]
                        ],
                    ),
                ]
            }
        )
    )

    rows = parse_team_box_rows(tmp_path)

    assert len(rows) == 1
    assert rows[0].nba_stats_team_id == "1610612753"
    assert rows[0].minutes == 200
    assert rows[0].pts == 106
    assert rows[0].off_rating == 115.2
    assert rows[0].pace == 110.84
