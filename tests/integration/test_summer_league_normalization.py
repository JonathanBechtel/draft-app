"""Integration tests for Summer League team/game normalization."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeagueDataQuality,
    SummerLeagueGame,
    SummerLeagueTeamEntry,
    SummerLeagueTeamGameLog,
)
from app.services.summer_league.audit import audit_summer_league_raw
from app.services.summer_league.normalization import normalize_competition_games


def _result_set(
    name: str, headers: list[str], rows: list[list[object]]
) -> dict[str, object]:
    return {"name": name, "headers": headers, "rowSet": rows}


def _write_fixture(raw_root: Path) -> None:
    run_dir = raw_root / "2024" / "15"
    game_dir = run_dir / "games" / "1522400001"
    game_dir.mkdir(parents=True)
    run_dir.joinpath("manifest.json").write_text(
        json.dumps(
            {
                "year": 2024,
                "league_id": "15",
                "venue": "las_vegas",
                "team_gamelog_rows": 2,
                "player_gamelog_rows": 0,
                "game_ids": ["1522400001"],
                "game_count": 1,
                "errors": [],
            }
        )
    )
    run_dir.joinpath("leaguegamelog_team.json").write_text(
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
                                1610612753,
                                "ORL",
                                "Orlando Magic",
                                "1522400001",
                                "2024-07-12",
                                "ORL vs. CLE",
                                106,
                            ],
                            [
                                1610612739,
                                "CLE",
                                "Cleveland Cavaliers",
                                "1522400001",
                                "2024-07-12",
                                "CLE @ ORL",
                                79,
                            ],
                        ],
                    )
                ]
            }
        )
    )
    run_dir.joinpath("leaguegamelog_player.json").write_text(
        json.dumps({"resultSets": [_result_set("LeagueGameLog", ["PLAYER_ID"], [])]})
    )
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
                                27,
                            ],
                            [
                                "1522400001",
                                1610612739,
                                "Cavaliers",
                                "CLE",
                                "200:00",
                                29,
                                81,
                                79,
                                -27,
                            ],
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
                            ],
                            [
                                "1522400001",
                                1610612739,
                                "Cavaliers",
                                "CLE",
                                85.9,
                                115.2,
                                110.84,
                            ],
                        ],
                    ),
                ]
            }
        )
    )
    game_dir.joinpath("boxscorescoringv2.json").write_text(
        json.dumps({"resultSets": []})
    )
    game_dir.joinpath("playbyplayv2.json").write_text(json.dumps({"resultSets": []}))
    game_dir.joinpath("shotchartdetail.json").write_text(json.dumps({"resultSets": []}))


@pytest.mark.asyncio
async def test_normalize_competition_games_is_idempotent(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """Normalizing a small audited fixture twice creates stable rows."""
    _write_fixture(tmp_path)
    await audit_summer_league_raw(
        db_session, raw_root=tmp_path, year=2024, league_id="15"
    )

    first = await normalize_competition_games(
        db_session, year=2024, league_id="15", raw_root=tmp_path
    )
    second = await normalize_competition_games(
        db_session, year=2024, league_id="15", raw_root=tmp_path
    )

    competition_count = await db_session.scalar(
        select(func.count()).select_from(SummerLeagueCompetition)
    )
    team_count = await db_session.scalar(
        select(func.count()).select_from(SummerLeagueTeamEntry)
    )
    game_count = await db_session.scalar(
        select(func.count()).select_from(SummerLeagueGame)
    )
    team_log_count = await db_session.scalar(
        select(func.count()).select_from(SummerLeagueTeamGameLog)
    )
    game = (await db_session.execute(select(SummerLeagueGame))).scalar_one()
    magic_log = (
        await db_session.execute(
            select(SummerLeagueTeamGameLog).where(SummerLeagueTeamGameLog.pts == 106)  # type: ignore[arg-type]
        )
    ).scalar_one()

    assert first.games_upserted == 1
    assert second.team_game_logs_upserted == 2
    assert competition_count == 1
    assert team_count == 2
    assert game_count == 1
    assert team_log_count == 2
    assert game.home_score == 106
    assert magic_log.off_rating == 115.2
    assert first.data_quality == SummerLeagueDataQuality.FULL
