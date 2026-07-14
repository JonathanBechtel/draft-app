"""Integration tests for Summer League team/game normalization."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeagueDataQuality,
    SummerLeagueGame,
    SummerLeagueGameStatus,
    SummerLeagueTeamEntry,
    SummerLeagueTeamGameLog,
)
from app.services.summer_league.audit import audit_summer_league_raw
from app.services.summer_league.normalization import (
    find_incomplete_team_box_game_ids,
    normalize_competition_games,
    refresh_competition_date_window,
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


def _rewrite_team_gamelog_pts(raw_root: Path, *, magic_pts: int) -> None:
    """Overwrite ``leaguegamelog_team.json``'s Magic row with a new PTS total.

    Used to simulate a live tick's targeted raw refresh (#531) landing a
    fresher season-gamelog snapshot between two normalize passes for the
    same still-in-progress game.
    """
    run_dir = raw_root / "2024" / "15"
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
                                magic_pts,
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


def _write_team_box(raw_root: Path, *, include_team_stats: bool) -> None:
    """(Re)write the fixture game's box-score files, with or without team rows.

    Mirrors ``_write_fixture``'s box-score payloads but is safe to call on
    its own (unlike ``_write_fixture``, which ``mkdir()``s without
    ``exist_ok`` and cannot be re-run against the same ``raw_root``). Used to
    simulate a per-game box fetched moments too early -- before NBA Stats
    posted the official box, so ``TeamStats`` comes back empty -- and then a
    later re-fetch landing the real rows.
    """
    game_dir = raw_root / "2024" / "15" / "games" / "1522400001"
    team_rows = (
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
        ]
        if include_team_stats
        else []
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
                        team_rows,
                    ),
                ]
            }
        )
    )


@pytest.mark.asyncio
async def test_find_incomplete_team_box_game_ids_flags_gamelog_fallback(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """A game whose team box came from the gamelog fallback is flagged for retry.

    Simulates a per-game box fetched before NBA Stats posted the official
    box (empty ``TeamStats``), forcing ``normalize_competition_games`` to
    build the team row from the season gamelog instead -- which never
    carries team minutes.
    """
    _write_fixture(tmp_path)
    _write_team_box(tmp_path, include_team_stats=False)
    await audit_summer_league_raw(
        db_session, raw_root=tmp_path, year=2024, league_id="15"
    )

    report = await normalize_competition_games(
        db_session, year=2024, league_id="15", raw_root=tmp_path
    )

    incomplete = await find_incomplete_team_box_game_ids(
        db_session, competition_id=report.competition_id
    )
    assert incomplete == ["1522400001"]

    team_logs = (await db_session.execute(select(SummerLeagueTeamGameLog))).scalars().all()
    assert len(team_logs) == 2
    assert all(log.minutes is None for log in team_logs)
    assert all(log.source_endpoint == "leaguegamelog_team" for log in team_logs)


@pytest.mark.asyncio
async def test_find_incomplete_team_box_game_ids_clears_once_box_score_lands(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """Re-normalizing after the real box score lands clears the flag.

    Models the ingest runner's retry pass (#573): force-refetch the
    still-incomplete game, then re-run normalization in the same run.
    """
    _write_fixture(tmp_path)
    _write_team_box(tmp_path, include_team_stats=False)
    await audit_summer_league_raw(
        db_session, raw_root=tmp_path, year=2024, league_id="15"
    )
    report = await normalize_competition_games(
        db_session, year=2024, league_id="15", raw_root=tmp_path
    )
    assert await find_incomplete_team_box_game_ids(
        db_session, competition_id=report.competition_id
    ) == ["1522400001"]

    _write_team_box(tmp_path, include_team_stats=True)
    await normalize_competition_games(
        db_session, year=2024, league_id="15", raw_root=tmp_path
    )

    assert (
        await find_incomplete_team_box_game_ids(
            db_session, competition_id=report.competition_id
        )
        == []
    )
    team_logs = (await db_session.execute(select(SummerLeagueTeamGameLog))).scalars().all()
    assert all(log.minutes == 200 for log in team_logs)
    assert all(log.source_endpoint == "boxscoretraditionalv2" for log in team_logs)


@pytest.mark.asyncio
async def test_normalize_competition_games_advances_scores_across_partial_passes_while_non_final(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """#530: team scores advance across two normalize passes while status stays non-Final.

    Mirrors what a live tick's targeted raw refresh (#531) followed by
    normalization does mid-event: each pass lands a fresher gamelog snapshot
    for a game scoreboard has already marked Scheduled, and the persisted
    score should track the latest snapshot -- without normalization ever
    promoting the game to Final on its own.
    """
    _write_fixture(tmp_path)
    (
        tmp_path / "2024" / "15" / "games" / "1522400001" / "shotchartdetail.json"
    ).unlink()
    await audit_summer_league_raw(
        db_session, raw_root=tmp_path, year=2024, league_id="15"
    )

    competition = SummerLeagueCompetition(
        year=2024,
        league_id="15",
        venue_slug="las_vegas",
        display_name="2024 Las Vegas Summer League",
    )
    db_session.add(competition)
    await db_session.flush()
    assert competition.id is not None
    db_session.add(
        SummerLeagueGame(
            competition_id=competition.id,
            nba_stats_game_id="1522400001",
            status=SummerLeagueGameStatus.SCHEDULED,
        )
    )
    await db_session.flush()

    # Pass 1 -- "first raw snapshot": Magic lead 50-ish, game still Scheduled
    # (no completion evidence: the audited run is PARTIAL, missing shotchart).
    _rewrite_team_gamelog_pts(tmp_path, magic_pts=52)
    await normalize_competition_games(
        db_session, year=2024, league_id="15", raw_root=tmp_path
    )
    first_game = (
        await db_session.execute(
            select(SummerLeagueGame).where(
                SummerLeagueGame.nba_stats_game_id == "1522400001"  # type: ignore[arg-type]
            )
        )
    ).scalar_one()
    assert first_game.home_score == 52
    assert first_game.status == SummerLeagueGameStatus.SCHEDULED

    # Pass 2 -- "second raw snapshot": score has moved on; still Scheduled.
    _rewrite_team_gamelog_pts(tmp_path, magic_pts=88)
    await normalize_competition_games(
        db_session, year=2024, league_id="15", raw_root=tmp_path
    )
    second_game = (
        await db_session.execute(
            select(SummerLeagueGame).where(
                SummerLeagueGame.nba_stats_game_id == "1522400001"  # type: ignore[arg-type]
            )
        )
    ).scalar_one()
    assert second_game.home_score == 88
    assert second_game.status == SummerLeagueGameStatus.SCHEDULED
    assert second_game.id == first_game.id  # same row, updated in place


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
    assert game.status == SummerLeagueGameStatus.FINAL


@pytest.mark.asyncio
async def test_normalize_competition_games_does_not_finalize_without_a_complete_audit(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """#530: a brand-new game normalized from a PARTIAL audited run stays Unknown.

    Missing completion evidence (here, a missing ``shotchartdetail.json`` --
    one of the five expected per-game endpoints -- makes the audited run
    PARTIAL, not COMPLETE) must never let normalization stamp a game Final
    on its own.
    """
    _write_fixture(tmp_path)
    (
        tmp_path / "2024" / "15" / "games" / "1522400001" / "shotchartdetail.json"
    ).unlink()
    await audit_summer_league_raw(
        db_session, raw_root=tmp_path, year=2024, league_id="15"
    )

    report = await normalize_competition_games(
        db_session, year=2024, league_id="15", raw_root=tmp_path
    )

    game = (await db_session.execute(select(SummerLeagueGame))).scalar_one()
    assert report.data_quality != SummerLeagueDataQuality.FULL
    assert game.status == SummerLeagueGameStatus.UNKNOWN


@pytest.mark.asyncio
async def test_normalize_competition_games_never_promotes_a_scoreboard_scheduled_game(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """#530: a game scoreboard already marked Scheduled stays Scheduled through normalize.

    Normalization is never the authority for a live game's
    Scheduled/In-Progress -> Final transition -- only scoreboard ingest
    (#529, Job B step 0) is, and by tick order it always runs before
    normalization. Proven here even against a fully COMPLETE audited run
    (the strongest completion evidence normalization ever sees), to show
    the guarantee holds regardless of raw-data completeness once scoreboard
    has already staked a non-Final status onto the row.
    """
    _write_fixture(tmp_path)
    await audit_summer_league_raw(
        db_session, raw_root=tmp_path, year=2024, league_id="15"
    )

    # Simulate scoreboard ingest having already tracked this exact game_id as
    # Scheduled before normalization ever runs (mirrors `upsert_scoreboard_games`
    # creating the row first; `_upsert_competition`'s (year, league_id) upsert
    # reuses this same competition row).
    competition = SummerLeagueCompetition(
        year=2024,
        league_id="15",
        venue_slug="las_vegas",
        display_name="2024 Las Vegas Summer League",
    )
    db_session.add(competition)
    await db_session.flush()
    assert competition.id is not None
    db_session.add(
        SummerLeagueGame(
            competition_id=competition.id,
            nba_stats_game_id="1522400001",
            status=SummerLeagueGameStatus.SCHEDULED,
        )
    )
    await db_session.flush()

    await normalize_competition_games(
        db_session, year=2024, league_id="15", raw_root=tmp_path
    )

    game = (
        await db_session.execute(
            select(SummerLeagueGame).where(
                SummerLeagueGame.nba_stats_game_id == "1522400001"  # type: ignore[arg-type]
            )
        )
    ).scalar_one()
    assert game.status == SummerLeagueGameStatus.SCHEDULED


@pytest.mark.asyncio
async def test_normalize_competition_games_keeps_final_monotonic_against_partial_audit(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """#530: a game already Final stays Final even when re-normalized from a PARTIAL audit.

    A later, less-complete raw refresh -- exactly what a live tick's
    targeted raw refresh can produce mid-event -- must never regress a
    proven-Final game back to Unknown/Scheduled/In-Progress.
    """
    _write_fixture(tmp_path)
    (
        tmp_path / "2024" / "15" / "games" / "1522400001" / "shotchartdetail.json"
    ).unlink()
    await audit_summer_league_raw(
        db_session, raw_root=tmp_path, year=2024, league_id="15"
    )

    competition = SummerLeagueCompetition(
        year=2024,
        league_id="15",
        venue_slug="las_vegas",
        display_name="2024 Las Vegas Summer League",
    )
    db_session.add(competition)
    await db_session.flush()
    assert competition.id is not None
    db_session.add(
        SummerLeagueGame(
            competition_id=competition.id,
            nba_stats_game_id="1522400001",
            status=SummerLeagueGameStatus.FINAL,
        )
    )
    await db_session.flush()

    await normalize_competition_games(
        db_session, year=2024, league_id="15", raw_root=tmp_path
    )

    game = (
        await db_session.execute(
            select(SummerLeagueGame).where(
                SummerLeagueGame.nba_stats_game_id == "1522400001"  # type: ignore[arg-type]
            )
        )
    ).scalar_one()
    assert game.status == SummerLeagueGameStatus.FINAL


@pytest.mark.asyncio
async def test_normalize_competition_games_sets_competition_date_window_from_games(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """#527/#528: normalizing a competition's games populates its date window.

    ``_upsert_competition`` never wrote ``starts_on``/``ends_on`` itself, so
    the Event Desk's opening-morning bootstrap (which derives a synthetic
    event window from those two fields) was permanently inert. After
    normalizing this fixture's one game (2024-07-12), the competition's
    window should equal that game's date on both ends.
    """
    _write_fixture(tmp_path)
    await audit_summer_league_raw(
        db_session, raw_root=tmp_path, year=2024, league_id="15"
    )

    await normalize_competition_games(
        db_session, year=2024, league_id="15", raw_root=tmp_path
    )

    competition = (
        await db_session.execute(
            select(SummerLeagueCompetition).where(
                SummerLeagueCompetition.year == 2024,  # type: ignore[arg-type]
                SummerLeagueCompetition.league_id == "15",  # type: ignore[arg-type]
            )
        )
    ).scalar_one()
    assert competition.starts_on == date(2024, 7, 12)
    assert competition.ends_on == date(2024, 7, 12)


@pytest.mark.asyncio
async def test_refresh_competition_date_window_spans_min_and_max_game_dates(
    db_session: AsyncSession,
) -> None:
    """Recomputing over several games sets the window to the true min/max.

    Seeds three games directly (bypassing the raw-file pipeline) with dates
    out of order, so a naive "last game wins" implementation would fail.
    """
    competition = SummerLeagueCompetition(
        year=2025,
        league_id="15",
        venue_slug="las_vegas",
        display_name="2025 Las Vegas Summer League",
    )
    db_session.add(competition)
    await db_session.flush()
    assert competition.id is not None

    for suffix, game_date in enumerate(
        [date(2025, 7, 15), date(2025, 7, 6), date(2025, 7, 20)]
    ):
        db_session.add(
            SummerLeagueGame(
                competition_id=competition.id,
                nba_stats_game_id=f"152250000{suffix}",
                game_date=game_date,
            )
        )
    await db_session.flush()

    await refresh_competition_date_window(db_session, competition_id=competition.id)

    assert competition.starts_on == date(2025, 7, 6)
    assert competition.ends_on == date(2025, 7, 20)


@pytest.mark.asyncio
async def test_refresh_competition_date_window_ignores_null_game_dates(
    db_session: AsyncSession,
) -> None:
    """A game row with no ``game_date`` yet must not corrupt the aggregate."""
    competition = SummerLeagueCompetition(
        year=2025,
        league_id="14",
        venue_slug="orlando",
        display_name="2025 Orlando Summer League",
    )
    db_session.add(competition)
    await db_session.flush()
    assert competition.id is not None

    db_session.add(
        SummerLeagueGame(
            competition_id=competition.id,
            nba_stats_game_id="1425000001",
            game_date=date(2025, 7, 10),
        )
    )
    db_session.add(
        SummerLeagueGame(
            competition_id=competition.id,
            nba_stats_game_id="1425000002",
            game_date=None,
        )
    )
    await db_session.flush()

    await refresh_competition_date_window(db_session, competition_id=competition.id)

    assert competition.starts_on == date(2025, 7, 10)
    assert competition.ends_on == date(2025, 7, 10)


@pytest.mark.asyncio
async def test_refresh_competition_date_window_leaves_null_with_zero_dated_games(
    db_session: AsyncSession,
) -> None:
    """A competition with no dated games yet keeps a null window (no crash).

    This is the residual #527 cold-start edge: a competition with zero games
    ever ingested still has no anchor for the synthetic-calendar bootstrap.
    That is expected and low-urgency -- this test only proves the helper
    itself is a safe no-op rather than nulling out or erroring.
    """
    competition = SummerLeagueCompetition(
        year=2027,
        league_id="15",
        venue_slug="las_vegas",
        display_name="2027 Las Vegas Summer League",
    )
    db_session.add(competition)
    await db_session.flush()
    assert competition.id is not None

    await refresh_competition_date_window(db_session, competition_id=competition.id)

    assert competition.starts_on is None
    assert competition.ends_on is None
