"""Integration tests for Summer League source-player and player-log normalization."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.player_affiliation import (
    AffiliationStatus,
    AffiliationType,
    PlayerAffiliation,
)
from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import (
    SummerLeagueParticipation,
    SummerLeaguePlayerGameLog,
    SummerLeagueResolutionStatus,
    SummerLeagueSourceRecord,
)
from app.services.player_mention_service import _normalized_name_key
from app.services.sources.summer_league.audit import audit_summer_league_raw
from app.services.sources.summer_league.normalization import (
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
        SummerLeagueSourceRecord(
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
        select(func.count()).select_from(SummerLeagueSourceRecord)
    )
    player_log_count = await db_session.scalar(
        select(func.count()).select_from(SummerLeaguePlayerGameLog)
    )
    unresolved_source = (
        await db_session.execute(
            select(SummerLeagueSourceRecord).where(
                SummerLeagueSourceRecord.nba_stats_person_id == "1640001"  # type: ignore[arg-type]
            )
        )
    ).scalar_one()
    resolved_source = (
        await db_session.execute(
            select(SummerLeagueSourceRecord).where(
                SummerLeagueSourceRecord.nba_stats_person_id == "1640002"  # type: ignore[arg-type]
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
    assert (
        unresolved_source.resolution_status == SummerLeagueResolutionStatus.UNRESOLVED
    )
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

    # The live ingest can arrive before a separate resolution pass. Its
    # temporary missing source link must not remove this completed game from
    # season metrics; a subsequent resolution pass remains authoritative.
    resolved_source.canonical_player_id = None
    await db_session.flush()
    await normalize_player_game_logs(
        db_session, year=2024, league_id="15", raw_root=tmp_path
    )
    await db_session.refresh(resolved_log)
    assert resolved_log.player_id == player.id


@pytest.mark.asyncio
async def test_normalize_player_game_logs_wires_participation_bridge(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """Every written game log references a stable participation bridge.

    Box-score-discovered players (no pre-event roster entry) get a CONFIRMED
    participation born canonical — with a matching CONFIRMED affiliation assertion
    (no orphan affiliation_id=None). Re-running normalization reuses the bridge
    (idempotent, no duplicate bridges or assertions); and an existing
    roster-announced bridge is reused without clobbering its roster_status.
    """
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
        SummerLeagueSourceRecord(
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

    await normalize_player_game_logs(
        db_session, year=2024, league_id="15", raw_root=tmp_path
    )
    # Idempotent re-run must not duplicate participation rows.
    await normalize_player_game_logs(
        db_session, year=2024, league_id="15", raw_root=tmp_path
    )

    logs = (await db_session.execute(select(SummerLeaguePlayerGameLog))).scalars().all()
    assert logs
    assert all(log.participation_id is not None for log in logs)

    participations = (
        (await db_session.execute(select(SummerLeagueParticipation))).scalars().all()
    )
    # Two written logs (the third row is skipped on team mismatch) → two bridges.
    assert len(participations) == 2
    assert all(p.roster_status == AffiliationStatus.CONFIRMED for p in participations)
    # Born canonical: each box-score bridge carries a real affiliation assertion,
    # not an orphan affiliation_id=None waiting on a reconcile pass.
    assert all(p.affiliation_id is not None for p in participations)
    affiliations = (await db_session.execute(select(PlayerAffiliation))).scalars().all()
    # Idempotent: the two assertions are not re-created on the second run.
    assert len(affiliations) == 2
    assert all(
        a.status == AffiliationStatus.CONFIRMED
        and a.affiliation_type == AffiliationType.SUMMER_LEAGUE_ROSTER
        and a.source == "nba_summer_league_box_score"
        for a in affiliations
    )
    assert {p.affiliation_id for p in participations} == {a.id for a in affiliations}

    by_source = {p.source_player_id: p for p in participations}
    resolved_log = (
        await db_session.execute(
            select(SummerLeaguePlayerGameLog).where(
                SummerLeaguePlayerGameLog.nba_stats_person_id == "1640002"  # type: ignore[arg-type]
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
    # Each log points at its own source-player bridge, and player_id mirrors.
    assert resolved_log.participation_id == by_source[resolved_log.source_player_id].id
    assert (
        unresolved_log.participation_id == by_source[unresolved_log.source_player_id].id
    )
    assert by_source[resolved_log.source_player_id].player_id == player.id
    assert by_source[unresolved_log.source_player_id].player_id is None

    # Simulate a roster-announced bridge: the normalizer must reuse it as-is and
    # never flip its roster_status (the affiliation stream is owned elsewhere).
    announced = participations[0]
    announced.roster_status = AffiliationStatus.ANNOUNCED
    await db_session.flush()
    await normalize_player_game_logs(
        db_session, year=2024, league_id="15", raw_root=tmp_path
    )
    await db_session.refresh(announced)
    assert announced.roster_status == AffiliationStatus.ANNOUNCED
    participation_count = await db_session.scalar(
        select(func.count()).select_from(SummerLeagueParticipation)
    )
    assert participation_count == 2

    # Resolution backfill after logs exist must propagate the canonical id onto
    # the previously-unresolved bridge on the next normalize pass.
    unresolved_source = (
        await db_session.execute(
            select(SummerLeagueSourceRecord).where(
                SummerLeagueSourceRecord.nba_stats_person_id == "1640001"  # type: ignore[arg-type]
            )
        )
    ).scalar_one()
    late_player = PlayerMaster(
        display_name="Unresolved Prospect", slug="unresolved-prospect"
    )
    db_session.add(late_player)
    await db_session.flush()
    unresolved_source.canonical_player_id = late_player.id
    await db_session.flush()
    await normalize_player_game_logs(
        db_session, year=2024, league_id="15", raw_root=tmp_path
    )
    backfilled = (
        await db_session.execute(
            select(SummerLeagueParticipation).where(
                SummerLeagueParticipation.source_player_id == unresolved_source.id  # type: ignore[arg-type]
            )
        )
    ).scalar_one()
    assert backfilled.player_id == late_player.id


def _write_season_log_fallback_fixture(
    raw_root: Path, *, include_per_game: bool
) -> None:
    """Fixture whose season LeagueGameLog carries a full box line (pre-2017 shape).

    Pre-2017 Summer League has no per-game boxscore data, so player logs are
    rebuilt from the season log. ``include_per_game`` optionally also writes a
    per-game boxscore for the same player-game (with a distinct PTS so tests can
    tell which source populated the row).
    """
    run_dir = raw_root / "2013" / "15"
    game_dir = run_dir / "games" / "1521300001"
    game_dir.mkdir(parents=True)
    run_dir.joinpath("manifest.json").write_text(
        json.dumps(
            {
                "year": 2013,
                "league_id": "15",
                "venue": "las_vegas",
                "team_gamelog_rows": 2,
                "player_gamelog_rows": 1,
                "game_ids": ["1521300001"],
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
                                1610612741,
                                "CHI",
                                "Chicago",
                                "1521300001",
                                "2013-07-13",
                                "CHI vs. MEM",
                                90,
                            ],
                            [
                                1610612763,
                                "MEM",
                                "Memphis",
                                "1521300001",
                                "2013-07-13",
                                "MEM @ CHI",
                                85,
                            ],
                        ],
                    )
                ]
            }
        )
    )
    # Season log carries GAME_ID + a full traditional line; PTS=20.
    run_dir.joinpath("leaguegamelog_player.json").write_text(
        json.dumps(
            {
                "resultSets": [
                    _result_set(
                        "LeagueGameLog",
                        [
                            "PLAYER_ID",
                            "PLAYER_NAME",
                            "TEAM_ID",
                            "GAME_ID",
                            "MIN",
                            "FGM",
                            "FGA",
                            "PTS",
                        ],
                        [
                            [
                                203503,
                                "Tony Snell",
                                1610612741,
                                "1521300001",
                                34,
                                6,
                                13,
                                20,
                            ]
                        ],
                    )
                ]
            }
        )
    )
    if include_per_game:
        # Same player-game via per-game boxscore, but PTS=99 to detect the source.
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
                                "MIN",
                                "FGM",
                                "FGA",
                                "PTS",
                            ],
                            [
                                [
                                    "1521300001",
                                    1610612741,
                                    203503,
                                    "Tony Snell",
                                    "34:00",
                                    6,
                                    13,
                                    99,
                                ]
                            ],
                        )
                    ]
                }
            )
        )


@pytest.mark.asyncio
async def test_season_log_fallback_fills_missing_player_games(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """With no per-game boxscore, player logs are rebuilt from the season log."""
    _write_season_log_fallback_fixture(tmp_path, include_per_game=False)
    await audit_summer_league_raw(
        db_session, raw_root=tmp_path, year=2013, league_id="15"
    )
    await normalize_competition_games(
        db_session, year=2013, league_id="15", raw_root=tmp_path
    )
    await normalize_player_game_logs(
        db_session, year=2013, league_id="15", raw_root=tmp_path
    )

    log = (await db_session.execute(select(SummerLeaguePlayerGameLog))).scalar_one()
    assert log.pts == 20  # sourced from the season log
    assert log.minutes_seconds == 34 * 60


@pytest.mark.asyncio
async def test_season_log_fallback_does_not_downgrade_existing_rows(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """Prevent a season-log fallback from overwriting a richer per-game row.

    This holds even when the per-game snapshot is absent on a later run.
    """
    _write_season_log_fallback_fixture(tmp_path, include_per_game=True)
    await audit_summer_league_raw(
        db_session, raw_root=tmp_path, year=2013, league_id="15"
    )
    await normalize_competition_games(
        db_session, year=2013, league_id="15", raw_root=tmp_path
    )
    # First pass: the per-game boxscore (PTS=99) sources the row.
    await normalize_player_game_logs(
        db_session, year=2013, league_id="15", raw_root=tmp_path
    )
    log = (await db_session.execute(select(SummerLeaguePlayerGameLog))).scalar_one()
    assert log.pts == 99

    # Delete the per-game boxscore, re-normalize: the season log (PTS=20) must not
    # overwrite the existing per-game-sourced row.
    tmp_path.joinpath(
        "2013", "15", "games", "1521300001", "boxscoretraditionalv2.json"
    ).unlink()
    await normalize_player_game_logs(
        db_session, year=2013, league_id="15", raw_root=tmp_path
    )

    logs = (await db_session.execute(select(SummerLeaguePlayerGameLog))).scalars().all()
    assert len(logs) == 1  # no duplicate
    assert logs[0].pts == 99  # preserved, not downgraded to the season-log line
