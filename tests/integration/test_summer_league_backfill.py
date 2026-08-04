"""Integration tests for Summer League backbone backfill orchestration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.player_external_ids import PlayerExternalId
from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import (
    SummerLeagueEdition,
    SummerLeagueGame,
    SummerLeaguePlayerGameLog,
    SummerLeagueSourcePlayer,
    SummerLeagueTeamEntry,
    SummerLeagueTeamGameLog,
)
from app.services.summer_league.backfill import (
    SummerLeagueBackfillOptions,
    backfill_summer_league_backbone,
)
from app.services.summer_league.player_resolution import NBA_STATS_SYSTEM


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
                "player_gamelog_rows": 2,
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
                            [1640001, "Exact Prospect", 1610612753],
                            [1640002, "Unmatched Prospect", 1610612739],
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
                            "PTS",
                            "PLUS_MINUS",
                        ],
                        [
                            [
                                "1522400001",
                                1610612753,
                                1640001,
                                "Exact Prospect",
                                "G",
                                "",
                                "24:28",
                                6,
                                11,
                                17,
                                8,
                            ],
                            [
                                "1522400001",
                                1610612739,
                                1640002,
                                "Unmatched Prospect",
                                "F",
                                "",
                                "18:00",
                                2,
                                7,
                                5,
                                -4,
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
                    _result_set("PlayerStats", [], []),
                    _result_set("TeamStats", [], []),
                ]
            }
        )
    )
    game_dir.joinpath("boxscorescoringv2.json").write_text(
        json.dumps({"resultSets": [_result_set("sqlPlayersScoring", [], [])]})
    )
    game_dir.joinpath("playbyplayv2.json").write_text(json.dumps({"resultSets": []}))
    game_dir.joinpath("shotchartdetail.json").write_text(json.dumps({"resultSets": []}))


async def _table_count(db_session: AsyncSession, table: type[object]) -> int:
    count = await db_session.scalar(select(func.count()).select_from(table))
    return int(count or 0)


@pytest.mark.asyncio
async def test_backfill_summer_league_backbone_is_idempotent(
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The full backfill creates stable rows and resolution links on rerun."""
    _write_fixture(tmp_path)
    player = PlayerMaster(display_name="Exact Prospect", slug="exact-prospect")
    db_session.add(player)
    await db_session.flush()
    assert player.id is not None

    async def fake_candidates(
        _db: AsyncSession,
        _query: str,
        k: int = 5,
    ) -> list[object]:
        return []

    monkeypatch.setattr(
        "app.services.summer_league.player_resolution.find_candidate_players",
        fake_candidates,
    )

    options = SummerLeagueBackfillOptions(
        year=2024,
        league_id="15",
        raw_root=tmp_path,
    )
    first = await backfill_summer_league_backbone(db_session, options)
    second = await backfill_summer_league_backbone(db_session, options)

    external_id = (
        await db_session.execute(
            select(PlayerExternalId).where(
                PlayerExternalId.system == NBA_STATS_SYSTEM,  # type: ignore[arg-type]
                PlayerExternalId.external_id == "1640001",  # type: ignore[arg-type]
            )
        )
    ).scalar_one()
    exact_log = (
        await db_session.execute(
            select(SummerLeaguePlayerGameLog).where(
                SummerLeaguePlayerGameLog.nba_stats_person_id == "1640001"  # type: ignore[arg-type]
            )
        )
    ).scalar_one()

    assert first.audit.runs_scanned == 1
    assert first.competition_games is not None
    assert first.competition_games.games_upserted == 1
    assert first.player_logs is not None
    assert first.player_logs.source_players_upserted == 2
    assert first.resolution is not None
    assert first.resolution.resolved_source_players == 1
    assert first.resolution.unresolved_source_players == 1
    assert second.competition_games is not None
    assert second.competition_games.team_game_logs_upserted == 2
    assert await _table_count(db_session, SummerLeagueEdition) == 1
    assert await _table_count(db_session, SummerLeagueTeamEntry) == 2
    assert await _table_count(db_session, SummerLeagueGame) == 1
    assert await _table_count(db_session, SummerLeagueTeamGameLog) == 2
    assert await _table_count(db_session, SummerLeagueSourcePlayer) == 2
    assert await _table_count(db_session, SummerLeaguePlayerGameLog) == 2
    assert await _table_count(db_session, PlayerExternalId) == 1
    assert external_id.player_id == player.id
    assert exact_log.player_id == player.id


@pytest.mark.asyncio
async def test_backfill_dry_run_rolls_back_database_changes(
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dry-run backfill reports counts without persisting generated rows."""
    _write_fixture(tmp_path)

    async def fake_candidates(
        _db: AsyncSession,
        _query: str,
        k: int = 5,
    ) -> list[object]:
        return []

    monkeypatch.setattr(
        "app.services.summer_league.player_resolution.find_candidate_players",
        fake_candidates,
    )

    report = await backfill_summer_league_backbone(
        db_session,
        SummerLeagueBackfillOptions(
            year=2024,
            league_id="15",
            raw_root=tmp_path,
            dry_run=True,
        ),
    )

    assert report.dry_run is True
    assert report.unsupported_dry_run_stages == ()
    assert report.player_logs is not None
    assert report.player_logs.player_game_logs_upserted == 2
    assert await _table_count(db_session, SummerLeagueEdition) == 0
    assert await _table_count(db_session, SummerLeagueSourcePlayer) == 0
    assert await _table_count(db_session, SummerLeaguePlayerGameLog) == 0
