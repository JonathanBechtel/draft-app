"""Integration tests for Summer League source-player and player-log normalization."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import (
    SummerLeaguePlayerGameLog,
    SummerLeagueResolutionStatus,
    SummerLeagueSourcePlayer,
)
from app.services.player_mention_service import _normalized_name_key
from app.services.summer_league.audit import audit_summer_league_raw
from app.services.summer_league.normalization import (
    normalize_competition_games,
    normalize_player_game_logs,
)


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
                "player_gamelog_rows": 3,
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
        json.dumps(
            {
                "resultSets": [
                    _result_set(
                        "LeagueGameLog",
                        ["PLAYER_ID", "PLAYER_NAME", "TEAM_ID"],
                        [
                            [1640001, "Unresolved Prospect", 1610612753],
                            [1640002, "Resolved Prospect", 1610612753],
                            [1640003, "Skipped Prospect", 9999999999],
                        ],
                    )
                ]
            }
        )
    )
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
                            "FG3M",
                            "FG3A",
                            "FG3_PCT",
                            "FTM",
                            "FTA",
                            "FT_PCT",
                            "OREB",
                            "DREB",
                            "REB",
                            "AST",
                            "STL",
                            "BLK",
                            "TO",
                            "PF",
                            "PTS",
                            "PLUS_MINUS",
                        ],
                        [
                            [
                                "1522400001",
                                1610612753,
                                1640001,
                                "Unresolved Prospect",
                                "G",
                                "",
                                "24:28",
                                6,
                                11,
                                0.545,
                                2,
                                4,
                                0.5,
                                3,
                                4,
                                0.75,
                                1,
                                3,
                                4,
                                5,
                                2,
                                1,
                                3,
                                2,
                                17,
                                8,
                            ],
                            [
                                "1522400001",
                                1610612753,
                                1640002,
                                "Resolved Prospect",
                                "",
                                "DNP - Coach's Decision",
                                "",
                                "",
                                "",
                                "",
                                "",
                                "",
                                "",
                                "",
                                "",
                                "",
                                "",
                                "",
                                "",
                                "",
                                "",
                                "",
                                "",
                                "",
                                "",
                                "",
                            ],
                            [
                                "1522400001",
                                9999999999,
                                1640003,
                                "Skipped Prospect",
                                "",
                                "",
                                "1:00",
                                0,
                                1,
                                0,
                                0,
                                0,
                                "",
                                0,
                                0,
                                "",
                                0,
                                0,
                                0,
                                0,
                                0,
                                0,
                                0,
                                0,
                                0,
                                -1,
                            ],
                        ],
                    ),
                    _result_set(
                        "TeamStats",
                        [
                            "GAME_ID",
                            "TEAM_ID",
                            "TEAM_NAME",
                            "TEAM_ABBREVIATION",
                            "MIN",
                            "PTS",
                        ],
                        [
                            [
                                "1522400001",
                                1610612753,
                                "Magic",
                                "ORL",
                                "200:00",
                                106,
                            ],
                            [
                                "1522400001",
                                1610612739,
                                "Cavaliers",
                                "CLE",
                                "200:00",
                                79,
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
                                "Unresolved Prospect",
                                119.1,
                                0.22,
                                0.14,
                            ]
                        ],
                    ),
                    _result_set("TeamStats", [], []),
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
                            "PCT_PTS_FT",
                        ],
                        [
                            [
                                "1522400001",
                                1610612753,
                                1640001,
                                "Unresolved Prospect",
                                0.636,
                                0.353,
                                0.176,
                            ]
                        ],
                    )
                ]
            }
        )
    )
    game_dir.joinpath("playbyplayv2.json").write_text(json.dumps({"resultSets": []}))
    game_dir.joinpath("shotchartdetail.json").write_text(json.dumps({"resultSets": []}))


@pytest.mark.asyncio
async def test_normalize_player_game_logs_is_idempotent_and_allows_unresolved(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """Player-log normalization preserves source identity and nullable links."""
    _write_fixture(tmp_path)
    await audit_summer_league_raw(
        db_session, raw_root=tmp_path, year=2024, league_id="15"
    )
    await normalize_competition_games(
        db_session, year=2024, league_id="15", raw_root=tmp_path
    )

    player = PlayerMaster(display_name="Resolved Prospect", slug="resolved-prospect")
    db_session.add(player)
    await db_session.flush()
    assert player.id is not None
    db_session.add(
        SummerLeagueSourcePlayer(
            nba_stats_person_id="1640002",
            raw_player_name="Resolved Prospect",
            normalized_name=_normalized_name_key("Resolved Prospect"),
            first_seen_year=2023,
            last_seen_year=2023,
            canonical_player_id=player.id,
            resolution_status=SummerLeagueResolutionStatus.EXTERNAL_ID,
            resolution_confidence=1.0,
            resolved_by="test",
        )
    )
    await db_session.flush()

    first = await normalize_player_game_logs(
        db_session, year=2024, league_id="15", raw_root=tmp_path
    )
    second = await normalize_player_game_logs(
        db_session, year=2024, league_id="15", raw_root=tmp_path
    )

    source_player_count = await db_session.scalar(
        select(func.count()).select_from(SummerLeagueSourcePlayer)
    )
    player_log_count = await db_session.scalar(
        select(func.count()).select_from(SummerLeaguePlayerGameLog)
    )
    unresolved_source = (
        await db_session.execute(
            select(SummerLeagueSourcePlayer).where(
                SummerLeagueSourcePlayer.nba_stats_person_id == "1640001"  # type: ignore[arg-type]
            )
        )
    ).scalar_one()
    resolved_source = (
        await db_session.execute(
            select(SummerLeagueSourcePlayer).where(
                SummerLeagueSourcePlayer.nba_stats_person_id == "1640002"  # type: ignore[arg-type]
            )
        )
    ).scalar_one()
    unresolved_log = (
        await db_session.execute(
            select(SummerLeaguePlayerGameLog).where(
                SummerLeaguePlayerGameLog.nba_stats_person_id == "1640001"  # type: ignore[arg-type]
            )
        )
    ).scalar_one()
    resolved_log = (
        await db_session.execute(
            select(SummerLeaguePlayerGameLog).where(
                SummerLeaguePlayerGameLog.nba_stats_person_id == "1640002"  # type: ignore[arg-type]
            )
        )
    ).scalar_one()

    assert first.source_players_upserted == 3
    assert first.player_game_logs_upserted == 2
    assert first.player_game_logs_skipped == 1
    assert second.player_game_logs_upserted == 2
    assert source_player_count == 3
    assert player_log_count == 2
    assert unresolved_source.canonical_player_id is None
    assert unresolved_source.resolution_status == SummerLeagueResolutionStatus.UNRESOLVED
    assert unresolved_log.source_player_id == unresolved_source.id
    assert unresolved_log.player_id is None
    assert unresolved_log.minutes_seconds == 1468
    assert unresolved_log.tov == 3
    assert unresolved_log.off_rating == 119.1
    assert unresolved_log.pct_fga_2pt == 0.636
    assert resolved_source.first_seen_year == 2023
    assert resolved_source.last_seen_year == 2024
    assert resolved_source.resolution_status == SummerLeagueResolutionStatus.EXTERNAL_ID
    assert resolved_log.source_player_id == resolved_source.id
    assert resolved_log.player_id == player.id
    assert resolved_log.comment == "DNP - Coach's Decision"
    assert resolved_log.minutes_seconds is None
