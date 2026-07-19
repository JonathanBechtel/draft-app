"""Integration tests for the shotchartdetail normalization stage.

Exercises ``normalize_shot_events`` against a real Postgres test schema and
asserts:
1. Shot events are upserted idempotently — a second parse yields the same row
   count with no duplicate (nba_stats_game_id, nba_stats_game_event_id) pairs.
2. An unresolved source player produces a shot event row with player_id IS NULL.
3. A resolved source player produces a shot event row with player_id set.
4. An empty shotchartdetail file (zero rows) leaves shotchart_available=False.

Requires TEST_DATABASE_URL and PYTEST_ALLOW_DB=1.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeagueRawFileStatus,
    SummerLeagueResolutionStatus,
    SummerLeagueShotEvent,
    SummerLeagueSourcePlayer,
)
from app.services.player_mention_service import _normalized_name_key
from app.services.summer_league.audit import audit_summer_league_raw
from app.services.summer_league.normalization import (
    normalize_competition_games,
    normalize_shot_events,
)


def _result_set(
    name: str, headers: list[str], rows: list[list[object]]
) -> dict[str, object]:
    return {"name": name, "headers": headers, "rowSet": rows}


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


def _shot_row(
    *,
    event_id: int,
    player_id: int,
    player_name: str,
    team_id: int = 1610612753,
    shot_made_flag: int = 1,
    shot_type: str = "3PT Field Goal",
    game_id: str = "1522400001",
) -> list[object]:
    return [
        "Shot Chart Detail",
        game_id,
        event_id,
        player_id,
        player_name,
        team_id,
        "Orlando Magic",
        1,
        9,
        30,
        "Made Shot" if shot_made_flag else "Missed Shot",
        "Jump Shot",
        shot_type,
        "Above the Break 3",
        "Left Side Center(LC)",
        "24+ ft.",
        25,
        -120,
        60,
        1,
        shot_made_flag,
        "2024-07-12",
        "ORL",
        "CLE",
    ]


def _write_season_skeleton(raw_root: Path) -> Path:
    """Write the common manifest + team gamelog files, return game dir."""
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
    # Minimal box-score files so normalization doesn't choke on missing files
    game_dir.joinpath("boxscoretraditionalv2.json").write_text(
        json.dumps(
            {
                "resultSets": [
                    _result_set(
                        "TeamStats",
                        ["GAME_ID", "TEAM_ID", "TEAM_NAME", "TEAM_ABBREVIATION", "MIN", "PTS"],
                        [
                            ["1522400001", 1610612753, "Magic", "ORL", "200:00", 106],
                            ["1522400001", 1610612739, "Cavaliers", "CLE", "200:00", 79],
                        ],
                    ),
                    _result_set("PlayerStats", [], []),
                ]
            }
        )
    )
    game_dir.joinpath("boxscoreadvancedv2.json").write_text(
        json.dumps({"resultSets": [_result_set("PlayerStats", [], []), _result_set("TeamStats", [], [])]})
    )
    game_dir.joinpath("boxscorescoringv2.json").write_text(
        json.dumps({"resultSets": [_result_set("sqlPlayersScoring", [], [])]})
    )
    game_dir.joinpath("playbyplayv2.json").write_text(json.dumps({"resultSets": []}))

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
    ``normalize_shot_events`` have more than one game to select between.
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
        game_dir.joinpath("playbyplayv2.json").write_text(json.dumps({"resultSets": []}))

    return game_dir_1, game_dir_2


@pytest.mark.asyncio
async def test_normalize_shot_events_upserts_and_is_idempotent(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """Shot events are inserted on first run and stable on second run.

    Expected: two shot rows (events 42 and 47), idempotent row count, unique
    (nba_stats_game_id, nba_stats_game_event_id) pairs preserved.
    """
    game_dir = _write_season_skeleton(tmp_path)
    game_dir.joinpath("shotchartdetail.json").write_text(
        json.dumps(
            {
                "resultSets": [
                    _result_set(
                        "Shot_Chart_Detail",
                        SHOT_HEADERS,
                        [
                            _shot_row(
                                event_id=42,
                                player_id=1640001,
                                player_name="Player A",
                                shot_made_flag=1,
                            ),
                            _shot_row(
                                event_id=47,
                                player_id=1640001,
                                player_name="Player A",
                                shot_made_flag=0,
                            ),
                        ],
                    )
                ]
            }
        )
    )
    await _setup_competition(db_session, tmp_path)

    report1 = await normalize_shot_events(
        db_session, year=2024, league_id="15", raw_root=tmp_path
    )
    count_after_first = await db_session.scalar(
        select(func.count()).select_from(SummerLeagueShotEvent)
    )

    report2 = await normalize_shot_events(
        db_session, year=2024, league_id="15", raw_root=tmp_path
    )
    count_after_second = await db_session.scalar(
        select(func.count()).select_from(SummerLeagueShotEvent)
    )

    assert report1.shot_events_upserted == 2
    assert report1.games_processed == 1
    assert report1.games_with_shots == 1
    assert count_after_first == 2

    # Idempotency: same report counts, same DB row count
    assert report2.shot_events_upserted == 2
    assert count_after_second == 2

    # No duplicate event IDs for the same game
    event_pairs = (
        await db_session.execute(
            select(
                SummerLeagueShotEvent.nba_stats_game_id,
                SummerLeagueShotEvent.nba_stats_game_event_id,
            )
        )
    ).all()
    assert len(event_pairs) == len(set(event_pairs))


@pytest.mark.asyncio
async def test_normalize_shot_events_unresolved_player_has_null_player_id(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """A source player with no canonical resolution yields player_id=NULL on the shot row."""
    game_dir = _write_season_skeleton(tmp_path)
    game_dir.joinpath("shotchartdetail.json").write_text(
        json.dumps(
            {
                "resultSets": [
                    _result_set(
                        "Shot_Chart_Detail",
                        SHOT_HEADERS,
                        [
                            _shot_row(
                                event_id=10,
                                player_id=9990001,
                                player_name="Unresolved Prospect",
                            )
                        ],
                    )
                ]
            }
        )
    )
    await _setup_competition(db_session, tmp_path)
    await normalize_shot_events(db_session, year=2024, league_id="15", raw_root=tmp_path)

    shot = (
        await db_session.execute(select(SummerLeagueShotEvent))
    ).scalar_one()
    source = (
        await db_session.execute(
            select(SummerLeagueSourcePlayer).where(
                SummerLeagueSourcePlayer.nba_stats_person_id == "9990001"  # type: ignore[arg-type]
            )
        )
    ).scalar_one()

    assert shot.player_id is None
    assert shot.source_player_id == source.id
    assert source.resolution_status == SummerLeagueResolutionStatus.UNRESOLVED
    assert source.canonical_player_id is None


@pytest.mark.asyncio
async def test_normalize_shot_events_resolved_player_sets_player_id(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """A source player with a canonical link populates player_id on the shot row."""
    game_dir = _write_season_skeleton(tmp_path)
    game_dir.joinpath("shotchartdetail.json").write_text(
        json.dumps(
            {
                "resultSets": [
                    _result_set(
                        "Shot_Chart_Detail",
                        SHOT_HEADERS,
                        [
                            _shot_row(
                                event_id=20,
                                player_id=1640002,
                                player_name="Resolved Prospect",
                            )
                        ],
                    )
                ]
            }
        )
    )
    await _setup_competition(db_session, tmp_path)

    # Pre-create the source player with a canonical link
    player = PlayerMaster(
        display_name="Resolved Prospect", slug="resolved-prospect"
    )
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

    await normalize_shot_events(db_session, year=2024, league_id="15", raw_root=tmp_path)

    shot = (
        await db_session.execute(select(SummerLeagueShotEvent))
    ).scalar_one()

    assert shot.player_id == player.id


def _write_season_log_player(game_dir: Path, headers: list[str], rows: list[list[object]]) -> None:
    """Overwrite the season leaguegamelog_player.json with real player lines."""
    run_dir = game_dir.parent.parent  # .../<year>/<league>
    run_dir.joinpath("leaguegamelog_player.json").write_text(
        json.dumps({"resultSets": [_result_set("LeagueGameLog", headers, rows)]})
    )


SEASON_LOG_HEADERS = [
    "GAME_ID",
    "TEAM_ID",
    "PLAYER_ID",
    "PLAYER_NAME",
    "MIN",
    "PTS",
    "FGM",
    "FGA",
    "FG3M",
    "FG3A",
]


@pytest.mark.asyncio
async def test_normalize_shot_events_legacy_id_crosswalks_to_canonical(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """A legacy shot-id remaps to the canonical box player via signature match (#467).

    The season log carries canonical player 203503 with a (FGA=2, FGM=1, 3PA=1,
    3PM=1) line; the shot chart carries legacy id 51845 with the same fingerprint
    (one made 3PT + one missed 2PT). The crosswalk rewrites the shot onto the
    canonical source player, so the shot inherits its resolved player_id and no
    stray legacy source player is created.
    """
    game_dir = _write_season_skeleton(tmp_path)
    _write_season_log_player(
        game_dir,
        SEASON_LOG_HEADERS,
        [["1522400001", 1610612753, 203503, "Tony Snell", "20:00", 8, 1, 2, 1, 1]],
    )
    game_dir.joinpath("shotchartdetail.json").write_text(
        json.dumps(
            {
                "resultSets": [
                    _result_set(
                        "Shot_Chart_Detail",
                        SHOT_HEADERS,
                        [
                            _shot_row(
                                event_id=101,
                                player_id=51845,
                                player_name="",
                                shot_made_flag=1,
                                shot_type="3PT Field Goal",
                            ),
                            _shot_row(
                                event_id=102,
                                player_id=51845,
                                player_name="",
                                shot_made_flag=0,
                                shot_type="2PT Field Goal",
                            ),
                        ],
                    )
                ]
            }
        )
    )
    await _setup_competition(db_session, tmp_path)

    # Canonical player resolved from the box side.
    player = PlayerMaster(display_name="Tony Snell", slug="tony-snell")
    db_session.add(player)
    await db_session.flush()
    assert player.id is not None
    db_session.add(
        SummerLeagueSourcePlayer(
            nba_stats_person_id="203503",
            raw_player_name="Tony Snell",
            normalized_name=_normalized_name_key("Tony Snell"),
            first_seen_year=2024,
            last_seen_year=2024,
            canonical_player_id=player.id,
            resolution_status=SummerLeagueResolutionStatus.EXTERNAL_ID,
            resolution_confidence=1.0,
            resolved_by="test",
        )
    )
    await db_session.flush()

    await normalize_shot_events(db_session, year=2024, league_id="15", raw_root=tmp_path)

    shots = (await db_session.execute(select(SummerLeagueShotEvent))).scalars().all()
    canonical_source = (
        await db_session.execute(
            select(SummerLeagueSourcePlayer).where(
                SummerLeagueSourcePlayer.nba_stats_person_id == "203503"  # type: ignore[arg-type]
            )
        )
    ).scalar_one()
    legacy_source = (
        await db_session.execute(
            select(SummerLeagueSourcePlayer).where(
                SummerLeagueSourcePlayer.nba_stats_person_id == "51845"  # type: ignore[arg-type]
            )
        )
    ).scalar_one_or_none()

    assert len(shots) == 2
    assert {s.nba_stats_person_id for s in shots} == {"203503"}
    assert all(s.source_player_id == canonical_source.id for s in shots)
    assert all(s.player_id == player.id for s in shots)
    # The legacy id never mints its own source player.
    assert legacy_source is None


@pytest.mark.asyncio
async def test_normalize_shot_events_unmatched_legacy_id_stays_unresolved(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """A legacy shot-id with no unique box fingerprint keeps its own (null) source.

    Two box players on one team share the (2,1,1,1) line, so the match is
    ambiguous; the crosswalk declines to guess and the legacy source player is
    created with player_id NULL rather than being mis-linked.
    """
    game_dir = _write_season_skeleton(tmp_path)
    _write_season_log_player(
        game_dir,
        SEASON_LOG_HEADERS,
        [
            ["1522400001", 1610612753, 203503, "Tony Snell", "20:00", 8, 1, 2, 1, 1],
            ["1522400001", 1610612753, 203999, "Other Guy", "18:00", 8, 1, 2, 1, 1],
        ],
    )
    game_dir.joinpath("shotchartdetail.json").write_text(
        json.dumps(
            {
                "resultSets": [
                    _result_set(
                        "Shot_Chart_Detail",
                        SHOT_HEADERS,
                        [
                            _shot_row(
                                event_id=201,
                                player_id=51845,
                                player_name="",
                                shot_made_flag=1,
                                shot_type="3PT Field Goal",
                            ),
                            _shot_row(
                                event_id=202,
                                player_id=51845,
                                player_name="",
                                shot_made_flag=0,
                                shot_type="2PT Field Goal",
                            ),
                        ],
                    )
                ]
            }
        )
    )
    await _setup_competition(db_session, tmp_path)
    await normalize_shot_events(db_session, year=2024, league_id="15", raw_root=tmp_path)

    shots = (await db_session.execute(select(SummerLeagueShotEvent))).scalars().all()
    assert len(shots) == 2
    assert {s.nba_stats_person_id for s in shots} == {"51845"}
    assert all(s.player_id is None for s in shots)


@pytest.mark.asyncio
async def test_normalize_shot_events_empty_file_leaves_flag_false(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """An empty shotchartdetail (zero shot rows) leaves shotchart_available=False.

    The competition starts with shotchart_available=False.  After normalization
    with a zero-row file, the flag must remain False and no shot events are
    inserted.
    """
    game_dir = _write_season_skeleton(tmp_path)
    game_dir.joinpath("shotchartdetail.json").write_text(
        json.dumps(
            {
                "resultSets": [
                    _result_set("Shot_Chart_Detail", SHOT_HEADERS, [])
                ]
            }
        )
    )
    await _setup_competition(db_session, tmp_path)
    report = await normalize_shot_events(
        db_session, year=2024, league_id="15", raw_root=tmp_path
    )

    competition_after = (
        await db_session.execute(select(SummerLeagueCompetition))
    ).scalar_one()
    shot_count = await db_session.scalar(
        select(func.count()).select_from(SummerLeagueShotEvent)
    )

    assert report.shot_events_upserted == 0
    assert report.games_with_shots == 0
    assert competition_after.shotchart_available is False
    assert shot_count == 0


@pytest.mark.asyncio
async def test_normalize_shot_events_sets_available_when_rows_exist(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """shotchart_available is set True when at least one shot row is parsed."""
    game_dir = _write_season_skeleton(tmp_path)
    game_dir.joinpath("shotchartdetail.json").write_text(
        json.dumps(
            {
                "resultSets": [
                    _result_set(
                        "Shot_Chart_Detail",
                        SHOT_HEADERS,
                        [_shot_row(event_id=1, player_id=1640001, player_name="P1")],
                    )
                ]
            }
        )
    )
    await _setup_competition(db_session, tmp_path)
    await normalize_shot_events(db_session, year=2024, league_id="15", raw_root=tmp_path)

    competition = (
        await db_session.execute(select(SummerLeagueCompetition))
    ).scalar_one()
    assert competition.shotchart_available is True


@pytest.mark.asyncio
async def test_normalize_shot_events_updates_raw_file_parse_status(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """SummerLeagueRawFile.parse_status is set to PARSED for the shotchartdetail endpoint."""
    from app.schemas.summer_league import SummerLeagueRawFile

    game_dir = _write_season_skeleton(tmp_path)
    game_dir.joinpath("shotchartdetail.json").write_text(
        json.dumps(
            {
                "resultSets": [
                    _result_set(
                        "Shot_Chart_Detail",
                        SHOT_HEADERS,
                        [_shot_row(event_id=5, player_id=1640001, player_name="P1")],
                    )
                ]
            }
        )
    )
    await _setup_competition(db_session, tmp_path)
    await normalize_shot_events(db_session, year=2024, league_id="15", raw_root=tmp_path)

    raw_file = (
        await db_session.execute(
            select(SummerLeagueRawFile).where(
                SummerLeagueRawFile.endpoint == "shotchartdetail",  # type: ignore[arg-type]
                SummerLeagueRawFile.game_id == "1522400001",  # type: ignore[arg-type]
            )
        )
    ).scalar_one_or_none()

    assert raw_file is not None
    assert raw_file.parse_status == SummerLeagueRawFileStatus.PARSED


@pytest.mark.asyncio
async def test_normalize_shot_events_game_ids_filters_to_explicit_batch(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """``game_ids`` restricts normalization to an explicit subset, e.g. one batch.

    Two games each carry a shot row. Calling with ``game_ids={game 1}`` only
    normalizes that game; a second call with ``game_ids={game 2}`` covers the
    rest with no duplicates -- proving the batched-call contract
    ``app.cli.summer_league_ingest_runner`` relies on (distinct from
    ``limit_games``, which selects a manifest-order prefix rather than an
    arbitrary subset).
    """
    game_dir_1, game_dir_2 = _write_two_game_season_skeleton(tmp_path)
    for game_dir, game_id, event_id, player_id, player_name in (
        (game_dir_1, "1522400001", 1, 1640001, "Player A"),
        (game_dir_2, "1522400002", 2, 1640002, "Player B"),
    ):
        game_dir.joinpath("shotchartdetail.json").write_text(
            json.dumps(
                {
                    "resultSets": [
                        _result_set(
                            "Shot_Chart_Detail",
                            SHOT_HEADERS,
                            [
                                _shot_row(
                                    event_id=event_id,
                                    player_id=player_id,
                                    player_name=player_name,
                                    game_id=game_id,
                                )
                            ],
                        )
                    ]
                }
            )
        )
    await _setup_competition(db_session, tmp_path)

    report1 = await normalize_shot_events(
        db_session,
        year=2024,
        league_id="15",
        raw_root=tmp_path,
        game_ids={"1522400001"},
    )
    assert report1.games_processed == 1
    assert report1.shot_events_upserted == 1

    shots_after_first = (
        (await db_session.execute(select(SummerLeagueShotEvent))).scalars().all()
    )
    assert {s.nba_stats_game_id for s in shots_after_first} == {"1522400001"}

    report2 = await normalize_shot_events(
        db_session,
        year=2024,
        league_id="15",
        raw_root=tmp_path,
        game_ids={"1522400002"},
    )
    assert report2.games_processed == 1
    assert report2.shot_events_upserted == 1

    shots_after_second = (
        (await db_session.execute(select(SummerLeagueShotEvent))).scalars().all()
    )
    assert {s.nba_stats_game_id for s in shots_after_second} == {
        "1522400001",
        "1522400002",
    }
    assert len(shots_after_second) == 2


@pytest.mark.asyncio
async def test_normalize_shot_events_batch_call_never_downgrades_availability_flag(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """A batch call with zero shots in its subset must not clear an already-set flag.

    Only a whole-venue call (``game_ids=None``) may set
    ``shotchart_available`` back to False -- a batch call (``game_ids`` set)
    must only ever raise it, never downgrade it just because *this batch's*
    games happened to have none while an earlier, already-committed batch
    did.
    """
    game_dir_1, game_dir_2 = _write_two_game_season_skeleton(tmp_path)
    game_dir_1.joinpath("shotchartdetail.json").write_text(
        json.dumps(
            {
                "resultSets": [
                    _result_set(
                        "Shot_Chart_Detail",
                        SHOT_HEADERS,
                        [_shot_row(event_id=1, player_id=1640001, player_name="Player A")],
                    )
                ]
            }
        )
    )
    game_dir_2.joinpath("shotchartdetail.json").write_text(
        json.dumps({"resultSets": [_result_set("Shot_Chart_Detail", SHOT_HEADERS, [])]})
    )
    await _setup_competition(db_session, tmp_path)

    await normalize_shot_events(
        db_session,
        year=2024,
        league_id="15",
        raw_root=tmp_path,
        game_ids={"1522400001"},
    )
    competition_after_first = (
        await db_session.execute(select(SummerLeagueCompetition))
    ).scalar_one()
    assert competition_after_first.shotchart_available is True

    await normalize_shot_events(
        db_session,
        year=2024,
        league_id="15",
        raw_root=tmp_path,
        game_ids={"1522400002"},
    )
    competition_after_second = (
        await db_session.execute(select(SummerLeagueCompetition))
    ).scalar_one()
    assert competition_after_second.shotchart_available is True
