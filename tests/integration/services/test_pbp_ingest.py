"""Integration tests for the playbyplayv2 PBP normalization stage.

Exercises ``normalize_pbp_events`` against a real Postgres test schema and
asserts:
1. PBP events are upserted idempotently — a second parse yields the same row
   count with no duplicate (nba_stats_game_id, event_num) pairs.
2. A game dir with no playbyplayv2.json yields zero events; pbp_available stays
   False.
3. An empty playbyplayv2 (zero rows) leaves pbp_available=False.
4. pbp_available is set True when at least one event row is parsed.
5. SummerLeagueRawFile.parse_status is set to PARSED for the playbyplayv2
   endpoint after parsing.

Requires TEST_DATABASE_URL and PYTEST_ALLOW_DB=1.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.summer_league import (
    SummerLeagueEdition,
    SummerLeaguePlayByPlayEvent,
    SummerLeagueRawFile,
    SummerLeagueRawFileStatus,
)
from app.services.summer_league.audit import audit_summer_league_raw
from app.services.summer_league.normalization import (
    normalize_competition_games,
    normalize_pbp_events,
)


def _result_set(
    name: str, headers: list[str], rows: list[list[object]]
) -> dict[str, object]:
    return {"name": name, "headers": headers, "rowSet": rows}


PBP_HEADERS = [
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


def _pbp_row(
    *,
    event_num: int,
    period: int = 1,
    pctimestring: str = "11:30",
    home_description: object = "Test Player A Jump Shot (2 PTS)",
    score: object = "2 - 0",
    score_margin: object = "2",
    player1_id: int = 1640001,
    player1_name: str = "Player A",
    game_id: str = "1522400001",
) -> list[object]:
    return [
        game_id,
        event_num,
        1,
        period,
        "7:00 PM",
        pctimestring,
        home_description,
        None,
        None,
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
        0,
        None,
        0,
        None,
        None,
        None,
        0,
        0,
        None,
        0,
        None,
        None,
        None,
        1,
    ]


def _write_season_skeleton(raw_root: Path) -> Path:
    """Write common manifest + team gamelog files; return the game directory."""
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
        json.dumps({"resultSets": []})
    )
    game_dir.joinpath("boxscoretraditionalv2.json").write_text(
        json.dumps(
            {
                "resultSets": [
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
                            ["1522400001", 1610612753, "Magic", "ORL", "200:00", 106],
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
                    _result_set("PlayerStats", [], []),
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
    game_dir.joinpath("shotchartdetail.json").write_text(
        json.dumps({"resultSets": []})
    )
    return game_dir


async def _setup_competition(db: AsyncSession, raw_root: Path) -> None:
    """Audit raw files and normalize competition/games."""
    await audit_summer_league_raw(db, raw_root=raw_root, year=2024, league_id="15")
    await normalize_competition_games(db, year=2024, league_id="15", raw_root=raw_root)
    await db.flush()


def _write_two_game_season_skeleton(raw_root: Path) -> tuple[Path, Path]:
    """Write manifest + team gamelog + minimal box files for two games.

    Mirrors :func:`_write_season_skeleton` but with a second game
    (``1522400002``) so ``game_ids``-filtered batch calls to
    ``normalize_pbp_events`` have more than one game to select between.
    """
    run_dir = raw_root / "2024" / "15"
    game_dir_1 = run_dir / "games" / "1522400001"
    game_dir_2 = run_dir / "games" / "1522400002"
    game_dir_1.mkdir(parents=True)
    game_dir_2.mkdir(parents=True)

    run_dir.joinpath("manifest.json").write_text(
        json.dumps(
            {
                "year": 2024,
                "league_id": "15",
                "venue": "las_vegas",
                "team_gamelog_rows": 4,
                "player_gamelog_rows": 0,
                "game_ids": ["1522400001", "1522400002"],
                "game_count": 2,
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
                            [
                                1610612753,
                                "ORL",
                                "Orlando Magic",
                                "1522400002",
                                "2024-07-13",
                                "ORL vs. CLE",
                                100,
                            ],
                            [
                                1610612739,
                                "CLE",
                                "Cleveland Cavaliers",
                                "1522400002",
                                "2024-07-13",
                                "CLE @ ORL",
                                90,
                            ],
                        ],
                    )
                ]
            }
        )
    )
    run_dir.joinpath("leaguegamelog_player.json").write_text(
        json.dumps({"resultSets": []})
    )

    for game_dir in (game_dir_1, game_dir_2):
        game_id = game_dir.name
        game_dir.joinpath("boxscoretraditionalv2.json").write_text(
            json.dumps(
                {
                    "resultSets": [
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
                                [game_id, 1610612753, "Magic", "ORL", "200:00", 106],
                                [game_id, 1610612739, "Cavaliers", "CLE", "200:00", 79],
                            ],
                        ),
                        _result_set("PlayerStats", [], []),
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
        game_dir.joinpath("shotchartdetail.json").write_text(
            json.dumps({"resultSets": []})
        )

    return game_dir_1, game_dir_2


@pytest.mark.asyncio
async def test_normalize_pbp_events_upserts_and_is_idempotent(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """PBP events are inserted on first run and stable on second run.

    Expected: three event rows (event_nums 1, 2, 3), idempotent row count,
    unique (nba_stats_game_id, event_num) pairs preserved across two runs.
    """
    game_dir = _write_season_skeleton(tmp_path)
    game_dir.joinpath("playbyplayv2.json").write_text(
        json.dumps(
            {
                "resultSets": [
                    _result_set(
                        "PlayByPlay",
                        PBP_HEADERS,
                        [
                            _pbp_row(event_num=1, score=None, score_margin=None),
                            _pbp_row(event_num=2, score="2 - 0", score_margin="2"),
                            _pbp_row(event_num=3, score="4 - 0", score_margin="4"),
                        ],
                    )
                ]
            }
        )
    )
    await _setup_competition(db_session, tmp_path)

    report1 = await normalize_pbp_events(
        db_session, year=2024, league_id="15", raw_root=tmp_path
    )
    count_after_first = await db_session.scalar(
        select(func.count()).select_from(SummerLeaguePlayByPlayEvent)
    )

    report2 = await normalize_pbp_events(
        db_session, year=2024, league_id="15", raw_root=tmp_path
    )
    count_after_second = await db_session.scalar(
        select(func.count()).select_from(SummerLeaguePlayByPlayEvent)
    )

    assert report1.pbp_events_upserted == 3
    assert report1.games_processed == 1
    assert report1.games_with_pbp == 1
    assert count_after_first == 3

    # Idempotency: same report counts, same DB row count
    assert report2.pbp_events_upserted == 3
    assert count_after_second == 3

    # No duplicate (game_id, event_num) pairs
    event_pairs = (
        await db_session.execute(
            select(
                SummerLeaguePlayByPlayEvent.nba_stats_game_id,
                SummerLeaguePlayByPlayEvent.event_num,
            )
        )
    ).all()
    assert len(event_pairs) == len(set(event_pairs))


@pytest.mark.asyncio
async def test_normalize_pbp_events_no_pbp_file_yields_zero_events(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """A game dir without playbyplayv2.json produces zero events and pbp_available=False.

    Expected: normalize_pbp_events processes zero games, inserts zero rows,
    and leaves pbp_available=False on the competition.
    """
    game_dir = _write_season_skeleton(tmp_path)
    # Deliberately omit playbyplayv2.json
    pbp_path = game_dir / "playbyplayv2.json"
    if pbp_path.exists():
        pbp_path.unlink()

    await _setup_competition(db_session, tmp_path)

    report = await normalize_pbp_events(
        db_session, year=2024, league_id="15", raw_root=tmp_path
    )

    competition = (
        await db_session.execute(select(SummerLeagueEdition))
    ).scalar_one()
    event_count = await db_session.scalar(
        select(func.count()).select_from(SummerLeaguePlayByPlayEvent)
    )

    assert report.pbp_events_upserted == 0
    assert report.games_processed == 0
    assert report.games_with_pbp == 0
    assert event_count == 0
    assert competition.pbp_available is False


@pytest.mark.asyncio
async def test_normalize_pbp_events_empty_file_leaves_flag_false(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """An empty playbyplayv2 (zero event rows) leaves pbp_available=False.

    Expected: the file exists and is parsed (games_processed=1), but zero
    rows are in it so games_with_pbp=0 and pbp_available stays False.
    """
    game_dir = _write_season_skeleton(tmp_path)
    game_dir.joinpath("playbyplayv2.json").write_text(
        json.dumps(
            {
                "resultSets": [
                    _result_set("PlayByPlay", PBP_HEADERS, [])
                ]
            }
        )
    )
    await _setup_competition(db_session, tmp_path)

    report = await normalize_pbp_events(
        db_session, year=2024, league_id="15", raw_root=tmp_path
    )

    competition = (
        await db_session.execute(select(SummerLeagueEdition))
    ).scalar_one()
    event_count = await db_session.scalar(
        select(func.count()).select_from(SummerLeaguePlayByPlayEvent)
    )

    assert report.pbp_events_upserted == 0
    assert report.games_processed == 1
    assert report.games_with_pbp == 0
    assert event_count == 0
    assert competition.pbp_available is False


@pytest.mark.asyncio
async def test_normalize_pbp_events_sets_available_when_rows_exist(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """pbp_available is set True when at least one event row is parsed."""
    game_dir = _write_season_skeleton(tmp_path)
    game_dir.joinpath("playbyplayv2.json").write_text(
        json.dumps(
            {
                "resultSets": [
                    _result_set(
                        "PlayByPlay",
                        PBP_HEADERS,
                        [_pbp_row(event_num=1, score=None, score_margin=None)],
                    )
                ]
            }
        )
    )
    await _setup_competition(db_session, tmp_path)

    await normalize_pbp_events(
        db_session, year=2024, league_id="15", raw_root=tmp_path
    )

    competition = (
        await db_session.execute(select(SummerLeagueEdition))
    ).scalar_one()
    assert competition.pbp_available is True


@pytest.mark.asyncio
async def test_normalize_pbp_events_updates_raw_file_parse_status(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """SummerLeagueRawFile.parse_status is set to PARSED for playbyplayv2."""
    game_dir = _write_season_skeleton(tmp_path)
    game_dir.joinpath("playbyplayv2.json").write_text(
        json.dumps(
            {
                "resultSets": [
                    _result_set(
                        "PlayByPlay",
                        PBP_HEADERS,
                        [_pbp_row(event_num=5, score="2 - 0", score_margin="2")],
                    )
                ]
            }
        )
    )
    await _setup_competition(db_session, tmp_path)
    await normalize_pbp_events(
        db_session, year=2024, league_id="15", raw_root=tmp_path
    )

    raw_file = (
        await db_session.execute(
            select(SummerLeagueRawFile).where(
                SummerLeagueRawFile.endpoint == "playbyplayv2",  # type: ignore[arg-type]
                SummerLeagueRawFile.game_id == "1522400001",  # type: ignore[arg-type]
            )
        )
    ).scalar_one_or_none()

    assert raw_file is not None
    assert raw_file.parse_status == SummerLeagueRawFileStatus.PARSED


@pytest.mark.asyncio
async def test_normalize_pbp_events_field_values_persisted(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """Event fields (period, clock, event_msg_type, scores, description) are stored correctly."""
    game_dir = _write_season_skeleton(tmp_path)
    game_dir.joinpath("playbyplayv2.json").write_text(
        json.dumps(
            {
                "resultSets": [
                    _result_set(
                        "PlayByPlay",
                        PBP_HEADERS,
                        [
                            _pbp_row(
                                event_num=7,
                                period=2,
                                pctimestring="5:30",
                                home_description="Player A Dunk Shot (2 PTS)",
                                score="10 - 8",
                                score_margin="2",
                                player1_id=1640001,
                                player1_name="Player A",
                            )
                        ],
                    )
                ]
            }
        )
    )
    await _setup_competition(db_session, tmp_path)
    await normalize_pbp_events(
        db_session, year=2024, league_id="15", raw_root=tmp_path
    )

    event = (
        await db_session.execute(
            select(SummerLeaguePlayByPlayEvent).where(
                SummerLeaguePlayByPlayEvent.event_num == 7  # type: ignore[arg-type]
            )
        )
    ).scalar_one()

    assert event.nba_stats_game_id == "1522400001"
    assert event.period == 2
    assert event.clock == "5:30"
    assert event.event_msg_type == 1
    assert event.home_score == 10
    assert event.away_score == 8
    assert event.score_margin == 2
    assert event.person1_nba_id == "1640001"
    assert event.description == "Player A Dunk Shot (2 PTS)"
    # No canonical player resolution since source player doesn't exist in test DB
    assert event.person1_id is None


@pytest.mark.asyncio
async def test_normalize_pbp_events_game_ids_filters_to_explicit_batch(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """``game_ids`` restricts normalization to an explicit subset, e.g. one batch.

    Two games each carry a PBP event. Calling with ``game_ids={game 1}``
    only normalizes that game; a second call with ``game_ids={game 2}``
    covers the rest with no duplicates -- proving the batched-call contract
    ``app.cli.summer_league_ingest_runner`` relies on (distinct from
    ``limit_games``, which selects a manifest-order prefix rather than an
    arbitrary subset).
    """
    game_dir_1, game_dir_2 = _write_two_game_season_skeleton(tmp_path)
    game_dir_1.joinpath("playbyplayv2.json").write_text(
        json.dumps(
            {
                "resultSets": [
                    _result_set(
                        "PlayByPlay",
                        PBP_HEADERS,
                        [
                            _pbp_row(
                                event_num=1,
                                score=None,
                                score_margin=None,
                                game_id="1522400001",
                            )
                        ],
                    )
                ]
            }
        )
    )
    game_dir_2.joinpath("playbyplayv2.json").write_text(
        json.dumps(
            {
                "resultSets": [
                    _result_set(
                        "PlayByPlay",
                        PBP_HEADERS,
                        [
                            _pbp_row(
                                event_num=1,
                                score=None,
                                score_margin=None,
                                game_id="1522400002",
                            )
                        ],
                    )
                ]
            }
        )
    )
    await _setup_competition(db_session, tmp_path)

    report1 = await normalize_pbp_events(
        db_session,
        year=2024,
        league_id="15",
        raw_root=tmp_path,
        game_ids={"1522400001"},
    )
    assert report1.games_processed == 1
    assert report1.pbp_events_upserted == 1

    events_after_first = (
        (await db_session.execute(select(SummerLeaguePlayByPlayEvent))).scalars().all()
    )
    assert {e.nba_stats_game_id for e in events_after_first} == {"1522400001"}

    report2 = await normalize_pbp_events(
        db_session,
        year=2024,
        league_id="15",
        raw_root=tmp_path,
        game_ids={"1522400002"},
    )
    assert report2.games_processed == 1
    assert report2.pbp_events_upserted == 1

    events_after_second = (
        (await db_session.execute(select(SummerLeaguePlayByPlayEvent))).scalars().all()
    )
    assert {e.nba_stats_game_id for e in events_after_second} == {
        "1522400001",
        "1522400002",
    }
    assert len(events_after_second) == 2
    # No duplicate (game_id, event_num) pairs across the two batch calls
    pairs = [(e.nba_stats_game_id, e.event_num) for e in events_after_second]
    assert len(pairs) == len(set(pairs))


@pytest.mark.asyncio
async def test_normalize_pbp_events_batch_call_never_downgrades_availability_flag(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """A batch call with zero PBP rows in its subset must not clear an already-set flag.

    Only a whole-venue call (``game_ids=None``) may set ``pbp_available``
    back to False -- a batch call (``game_ids`` set) must only ever raise
    it, never downgrade it just because *this batch's* games happened to
    have none while an earlier, already-committed batch did.
    """
    game_dir_1, game_dir_2 = _write_two_game_season_skeleton(tmp_path)
    game_dir_1.joinpath("playbyplayv2.json").write_text(
        json.dumps(
            {
                "resultSets": [
                    _result_set(
                        "PlayByPlay",
                        PBP_HEADERS,
                        [_pbp_row(event_num=1, score=None, score_margin=None)],
                    )
                ]
            }
        )
    )
    game_dir_2.joinpath("playbyplayv2.json").write_text(
        json.dumps({"resultSets": [_result_set("PlayByPlay", PBP_HEADERS, [])]})
    )
    await _setup_competition(db_session, tmp_path)

    await normalize_pbp_events(
        db_session,
        year=2024,
        league_id="15",
        raw_root=tmp_path,
        game_ids={"1522400001"},
    )
    competition_after_first = (
        await db_session.execute(select(SummerLeagueEdition))
    ).scalar_one()
    assert competition_after_first.pbp_available is True

    await normalize_pbp_events(
        db_session,
        year=2024,
        league_id="15",
        raw_root=tmp_path,
        game_ids={"1522400002"},
    )
    competition_after_second = (
        await db_session.execute(select(SummerLeagueEdition))
    ).scalar_one()
    assert competition_after_second.pbp_available is True
