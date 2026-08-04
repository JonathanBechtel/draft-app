"""DB-round-trip budget for the bulk shot/PBP normalization rewrite (#627).

The production incident that motivated #627: a single venue's shot-event
normalization pass issued one ``SELECT`` (source player) + one ``flush()`` +
one more ``SELECT``-then-write (shot event) *per shot row* -- 10,155 shot
events took 87.7 minutes, almost entirely round-trip latency rather than
query cost. ``normalize_shot_events``/``normalize_pbp_events`` were rewritten
to preload identities once per call and write via chunked
``INSERT ... ON CONFLICT`` instead (see
``app/services/summer_league/normalization.py``).

This test builds a synthetic 70-game, ~10k-shot-event fixture (the same
order of magnitude as the incident) and asserts the rewritten function's
*query count* -- not wall-clock, which is noisy on shared/CI hardware, see
``tests/integration/perf/budgets.py``'s rationale -- stays a small constant
multiple of the number of 500-row write chunks, nowhere near the
row-by-row baseline. The pre-refactor implementation issued at least two
statements per shot row (one source-player SELECT, one shot-event
SELECT-then-write); for 10,150 shots that is a documented floor of >=20,300
statements. The rewritten call should need well under 100.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.services.sources.summer_league.audit import audit_summer_league_raw
from app.services.sources.summer_league.normalization import (
    BULK_UPSERT_CHUNK_SIZE,
    normalize_competition_games,
    normalize_pbp_events,
    normalize_shot_events,
)
from tests.integration.perf._capture import count_queries

pytestmark = pytest.mark.asyncio

YEAR = 2024
LEAGUE_ID = "15"
NUM_GAMES = 70
SHOTS_PER_GAME = 145  # 70 * 145 = 10,150 shots -- same order as the incident.
PBP_PER_GAME = 20
TEAM_A = 1610612753
TEAM_B = 1610612739
# 5 players per team reused across every game -- a handful of distinct
# identities behind thousands of rows, exactly the shape the batched
# preload is meant to collapse into one statement.
TEAM_A_PLAYERS = [1640001, 1640002, 1640003, 1640004, 1640005]
TEAM_B_PLAYERS = [1640011, 1640012, 1640013, 1640014, 1640015]


def _result_set(
    name: str, headers: list[str], rows: list[list[object]]
) -> dict[str, object]:
    return {"name": name, "headers": headers, "rowSet": rows}


def _game_id(index: int) -> str:
    return f"15224{index:05d}"


SHOT_HEADERS = [
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


def _shot_row(
    *, game_id: str, event_id: int, player_id: int, team_id: int
) -> list[object]:
    return [
        "Shot Chart Detail",
        game_id,
        event_id,
        player_id,
        f"Player {player_id}",
        team_id,
        "Team",
        1,
        9,
        30,
        "Made Shot",
        "Jump Shot",
        "2PT Field Goal",
        "Mid-Range",
        "Center(C)",
        "16-24 ft.",
        18,
        0,
        180,
        1,
        1,
        "2024-07-12",
        "ORL",
        "CLE",
    ]


def _pbp_row(*, game_id: str, event_num: int, player_id: int) -> list[object]:
    return [
        game_id,
        event_num,
        1,
        1,
        "7:00 PM",
        "11:30",
        f"Player {player_id} Jump Shot (2 PTS)",
        None,
        None,
        "2 - 0",
        "2",
        4,
        player_id,
        f"Player {player_id}",
        TEAM_A,
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
        0,
    ]


def _write_bulk_fixture(raw_root: Path) -> None:
    """Write a 70-game raw fixture with ~10,150 shots and ~1,400 PBP events."""
    run_dir = raw_root / f"{YEAR}/{LEAGUE_ID}"
    game_ids = [_game_id(i) for i in range(NUM_GAMES)]

    team_gamelog_rows = []
    for game_id in game_ids:
        team_gamelog_rows.append(
            [TEAM_A, "ORL", "Orlando Magic", game_id, "2024-07-12", "ORL vs. CLE", 106]
        )
        team_gamelog_rows.append(
            [
                TEAM_B,
                "CLE",
                "Cleveland Cavaliers",
                game_id,
                "2024-07-12",
                "CLE @ ORL",
                79,
            ]
        )

    run_dir.mkdir(parents=True, exist_ok=True)
    run_dir.joinpath("manifest.json").write_text(
        json.dumps(
            {
                "year": YEAR,
                "league_id": LEAGUE_ID,
                "venue": "las_vegas",
                "team_gamelog_rows": len(team_gamelog_rows),
                "player_gamelog_rows": 0,
                "game_ids": game_ids,
                "game_count": len(game_ids),
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
                        team_gamelog_rows,
                    )
                ]
            }
        )
    )
    run_dir.joinpath("leaguegamelog_player.json").write_text(
        json.dumps({"resultSets": []})
    )

    for game_id in game_ids:
        game_dir = run_dir / "games" / game_id
        game_dir.mkdir(parents=True)
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
                                [game_id, TEAM_A, "Magic", "ORL", "200:00", 106],
                                [game_id, TEAM_B, "Cavaliers", "CLE", "200:00", 79],
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

        shot_rows = [
            _shot_row(
                game_id=game_id,
                event_id=event_id,
                player_id=(
                    TEAM_A_PLAYERS[event_id % len(TEAM_A_PLAYERS)]
                    if event_id % 2 == 0
                    else TEAM_B_PLAYERS[event_id % len(TEAM_B_PLAYERS)]
                ),
                team_id=TEAM_A if event_id % 2 == 0 else TEAM_B,
            )
            for event_id in range(SHOTS_PER_GAME)
        ]
        game_dir.joinpath("shotchartdetail.json").write_text(
            json.dumps(
                {
                    "resultSets": [
                        _result_set("Shot_Chart_Detail", SHOT_HEADERS, shot_rows)
                    ]
                }
            )
        )

        pbp_rows = [
            _pbp_row(
                game_id=game_id,
                event_num=event_num,
                player_id=TEAM_A_PLAYERS[event_num % len(TEAM_A_PLAYERS)],
            )
            for event_num in range(PBP_PER_GAME)
        ]
        game_dir.joinpath("playbyplayv2.json").write_text(
            json.dumps(
                {"resultSets": [_result_set("PlayByPlay", PBP_HEADERS, pbp_rows)]}
            )
        )


async def test_bulk_shot_normalization_query_count_stays_flat_at_70_games(
    db_session: AsyncSession,
    async_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    """~10,150 shots across 70 games normalize in a small, chunk-bounded query count.

    The pre-refactor row-by-row implementation issued at least two DB
    statements per shot row (one source-player SELECT, one shot-event
    SELECT-then-write) -- a documented floor of >= 2 * 10,150 = 20,300 for
    this fixture. The rewritten call preloads identities once and writes in
    ``BULK_UPSERT_CHUNK_SIZE``-row chunks, so its count should be a small
    constant multiple of ``10,150 / 500 ~= 21`` chunks -- asserted here at a
    generous 100 to stay a meaningful regression ratchet without being
    brittle to incidental preload/report-side queries.
    """
    _write_bulk_fixture(tmp_path)
    await audit_summer_league_raw(
        db_session, raw_root=tmp_path, year=YEAR, league_id=LEAGUE_ID
    )
    await normalize_competition_games(
        db_session, year=YEAR, league_id=LEAGUE_ID, raw_root=tmp_path
    )
    await db_session.flush()
    await db_session.commit()

    with count_queries(async_engine) as captured:
        report = await normalize_shot_events(
            db_session, year=YEAR, league_id=LEAGUE_ID, raw_root=tmp_path
        )
        await db_session.commit()

    assert report.games_processed == NUM_GAMES
    assert report.shot_events_upserted == NUM_GAMES * SHOTS_PER_GAME

    naive_row_by_row_floor = 2 * NUM_GAMES * SHOTS_PER_GAME
    assert len(captured) < naive_row_by_row_floor
    assert len(captured) < 100, (
        f"normalize_shot_events issued {len(captured)} statements for "
        f"{NUM_GAMES * SHOTS_PER_GAME} shots -- expected a small constant "
        f"multiple of {NUM_GAMES * SHOTS_PER_GAME // BULK_UPSERT_CHUNK_SIZE + 1} "
        "write chunks, not one that scales with row count."
    )


async def test_bulk_pbp_normalization_query_count_stays_flat_at_70_games(
    db_session: AsyncSession,
    async_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    """~1,400 PBP events across 70 games normalize in a small, chunk-bounded query count.

    Mirrors the shot-event budget above: the pre-refactor implementation
    issued up to three actor-resolution SELECTs plus one event
    SELECT-then-write per PBP row.
    """
    _write_bulk_fixture(tmp_path)
    await audit_summer_league_raw(
        db_session, raw_root=tmp_path, year=YEAR, league_id=LEAGUE_ID
    )
    await normalize_competition_games(
        db_session, year=YEAR, league_id=LEAGUE_ID, raw_root=tmp_path
    )
    await db_session.flush()
    await db_session.commit()

    with count_queries(async_engine) as captured:
        report = await normalize_pbp_events(
            db_session, year=YEAR, league_id=LEAGUE_ID, raw_root=tmp_path
        )
        await db_session.commit()

    assert report.games_processed == NUM_GAMES
    assert report.pbp_events_upserted == NUM_GAMES * PBP_PER_GAME

    naive_row_by_row_floor = NUM_GAMES * PBP_PER_GAME
    assert len(captured) < naive_row_by_row_floor
    assert len(captured) < 50, (
        f"normalize_pbp_events issued {len(captured)} statements for "
        f"{NUM_GAMES * PBP_PER_GAME} PBP events -- expected a small constant "
        "number of chunked statements, not one that scales with row count."
    )
