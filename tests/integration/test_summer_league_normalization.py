"""Integration tests for Summer League team/game normalization."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import (
    SummerLeagueEdition,
    SummerLeagueDataQuality,
    SummerLeagueGame,
    SummerLeagueGameStatus,
    SummerLeaguePlayByPlayEvent,
    SummerLeagueResolutionStatus,
    SummerLeagueShotEvent,
    SummerLeagueSourceRecord,
    SummerLeagueTeamEntry,
    SummerLeagueTeamGameLog,
)
from app.services.player_mention_service import _normalized_name_key
from app.services.sources.summer_league.audit import audit_summer_league_raw
from app.services.sources.summer_league.normalization import (
    find_incomplete_team_box_game_ids,
    normalize_competition_games,
    normalize_pbp_events,
    normalize_shot_events,
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


def _write_team_box(
    raw_root: Path, *, include_team_stats: bool, team_minutes: str = "200:00"
) -> None:
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
                team_minutes,
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
                team_minutes,
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
    db_session.add(
        SummerLeagueGame(
            competition_id=report.competition_id,
            nba_stats_game_id="1522400099",
            status=SummerLeagueGameStatus.SCHEDULED,
        )
    )
    db_session.add(
        SummerLeagueGame(
            competition_id=report.competition_id,
            nba_stats_game_id="1522400098",
            status=SummerLeagueGameStatus.FINAL,
        )
    )
    await db_session.flush()

    incomplete = await find_incomplete_team_box_game_ids(
        db_session, competition_id=report.competition_id
    )
    # The fallback and final zero-row games retry; the scheduled zero-row game does not.
    assert incomplete == ["1522400001", "1522400098"]

    team_logs = (
        (await db_session.execute(select(SummerLeagueTeamGameLog))).scalars().all()
    )
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
    team_logs = (
        (await db_session.execute(select(SummerLeagueTeamGameLog))).scalars().all()
    )
    assert all(log.minutes == 200 for log in team_logs)
    assert all(log.source_endpoint == "boxscoretraditionalv2" for log in team_logs)


@pytest.mark.asyncio
async def test_normalize_competition_games_never_downgrades_official_team_box(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """A later partial pass cannot replace an official box with the fallback."""
    _write_fixture(tmp_path)
    _write_team_box(tmp_path, include_team_stats=True)
    await audit_summer_league_raw(
        db_session, raw_root=tmp_path, year=2024, league_id="15"
    )
    report = await normalize_competition_games(
        db_session, year=2024, league_id="15", raw_root=tmp_path
    )

    _write_team_box(tmp_path, include_team_stats=False)
    await normalize_competition_games(
        db_session, year=2024, league_id="15", raw_root=tmp_path
    )

    team_logs = (
        (
            await db_session.execute(
                select(SummerLeagueTeamGameLog).where(
                    SummerLeagueTeamGameLog.competition_id == report.competition_id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(team_logs) == 2
    assert all(log.minutes == 200 for log in team_logs)
    assert all(log.source_endpoint == "boxscoretraditionalv2" for log in team_logs)


@pytest.mark.asyncio
async def test_find_incomplete_team_box_game_ids_flags_partial_official_box(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """Official team rows below the metrics minutes floor remain retryable."""
    _write_fixture(tmp_path)
    _write_team_box(tmp_path, include_team_stats=True, team_minutes="100:00")
    await audit_summer_league_raw(
        db_session, raw_root=tmp_path, year=2024, league_id="15"
    )

    report = await normalize_competition_games(
        db_session, year=2024, league_id="15", raw_root=tmp_path
    )

    assert await find_incomplete_team_box_game_ids(
        db_session, competition_id=report.competition_id
    ) == ["1522400001"]
    team_logs = (
        (await db_session.execute(select(SummerLeagueTeamGameLog))).scalars().all()
    )
    assert all(log.minutes == 100 for log in team_logs)
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

    competition = SummerLeagueEdition(
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


def _rewrite_team_gamelog_empty(raw_root: Path) -> None:
    """Overwrite ``leaguegamelog_team.json`` with zero rows.

    Simulates the season-wide LeagueGameLog feed not having caught up to a
    game yet, even though that game's per-game TeamStats box is already on
    disk (#633) -- the July 19, 2026 incident's actual failure shape.
    """
    run_dir = raw_root / "2024" / "15"
    run_dir.joinpath("leaguegamelog_team.json").write_text(
        json.dumps({"resultSets": [_result_set("LeagueGameLog", [], [])]})
    )


@pytest.mark.asyncio
async def test_normalize_competition_games_populates_score_from_teamstats_when_gamelog_lags(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """#633: a lagging season LeagueGameLog still gets a canonical score from TeamStats.

    Mirrors the real production shape: scoreboard ingest (Job B step 0, which
    always runs ahead of normalization) has already created the canonical
    game and linked its raw provider ``home_nba_stats_team_id``/
    ``away_nba_stats_team_id``, but the season-wide LeagueGameLog feed hasn't
    reported this game yet this pass -- while its per-game TeamStats box
    already has real scores on disk. Normalization must use the full
    competition's canonical game/team mappings (not just this batch's
    gamelog rows) to find the game and populate its score from that box data.
    """
    _write_fixture(tmp_path)
    await audit_summer_league_raw(
        db_session, raw_root=tmp_path, year=2024, league_id="15"
    )
    _rewrite_team_gamelog_empty(tmp_path)

    competition = SummerLeagueEdition(
        year=2024,
        league_id="15",
        venue_slug="las_vegas",
        display_name="2024 Las Vegas Summer League",
    )
    db_session.add(competition)
    await db_session.flush()
    assert competition.id is not None

    orl = SummerLeagueTeamEntry(
        competition_id=competition.id,
        nba_stats_team_id="1610612753",
        raw_team_name="Orlando Magic",
        team_slug="orlando-magic",
    )
    cle = SummerLeagueTeamEntry(
        competition_id=competition.id,
        nba_stats_team_id="1610612739",
        raw_team_name="Cleveland Cavaliers",
        team_slug="cleveland-cavaliers",
    )
    db_session.add_all([orl, cle])
    await db_session.flush()
    assert orl.id is not None and cle.id is not None

    db_session.add(
        SummerLeagueGame(
            competition_id=competition.id,
            nba_stats_game_id="1522400001",
            status=SummerLeagueGameStatus.SCHEDULED,
            tip_datetime=datetime(2024, 7, 12, 20, 0),
            home_nba_stats_team_id="1610612753",
            away_nba_stats_team_id="1610612739",
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
    assert game.home_score == 106
    assert game.away_score == 79
    # Score-population is normalization's job; live/passed-tip status display
    # is the read layer's (see `desk_read._effective_game_status`) --
    # normalize never touches status on its own here.
    assert game.status == SummerLeagueGameStatus.SCHEDULED

    team_logs = (
        (
            await db_session.execute(
                select(SummerLeagueTeamGameLog).where(
                    SummerLeagueTeamGameLog.game_id == game.id  # type: ignore[arg-type]
                )
            )
        )
        .scalars()
        .all()
    )
    assert {log.team_entry_id for log in team_logs} == {orl.id, cle.id}

    # Once the season LeagueGameLog catches up with the real value, it takes
    # precedence over the TeamStats fallback that seeded this game earlier.
    _rewrite_team_gamelog_pts(tmp_path, magic_pts=112)
    await normalize_competition_games(
        db_session, year=2024, league_id="15", raw_root=tmp_path
    )
    updated_game = (
        await db_session.execute(
            select(SummerLeagueGame).where(
                SummerLeagueGame.nba_stats_game_id == "1522400001"  # type: ignore[arg-type]
            )
        )
    ).scalar_one()
    assert updated_game.home_score == 112
    assert updated_game.away_score == 79
    assert updated_game.id == game.id


@pytest.mark.asyncio
async def test_normalize_competition_games_teamstats_fallback_never_clobbers_existing_score(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """#633: the TeamStats fallback must not overwrite an already-persisted score.

    Codex review on PR #652: the full-ingestion path treats an already-captured
    per-game TeamStats file as permanent (``force=False`` skips re-fetching it),
    so it can go stale relative to the game's actual current state -- e.g. a
    scoreboard-ingest live read of ``scheduleleaguev2`` (or an earlier pass of
    this same fallback) has already set a real, more current score. Re-running
    the fallback from that stale on-disk snapshot must never regress it.
    """
    _write_fixture(tmp_path)
    await audit_summer_league_raw(
        db_session, raw_root=tmp_path, year=2024, league_id="15"
    )
    _rewrite_team_gamelog_empty(tmp_path)

    competition = SummerLeagueEdition(
        year=2024,
        league_id="15",
        venue_slug="las_vegas",
        display_name="2024 Las Vegas Summer League",
    )
    db_session.add(competition)
    await db_session.flush()
    assert competition.id is not None

    orl = SummerLeagueTeamEntry(
        competition_id=competition.id,
        nba_stats_team_id="1610612753",
        raw_team_name="Orlando Magic",
        team_slug="orlando-magic",
    )
    cle = SummerLeagueTeamEntry(
        competition_id=competition.id,
        nba_stats_team_id="1610612739",
        raw_team_name="Cleveland Cavaliers",
        team_slug="cleveland-cavaliers",
    )
    db_session.add_all([orl, cle])
    await db_session.flush()

    # Scoreboard ingest already wrote a real, more current score (e.g. a
    # since-finished game) before this normalize pass ever runs. The fixture's
    # on-disk TeamStats box (106-79, written once and never re-fetched) is
    # stale relative to this.
    db_session.add(
        SummerLeagueGame(
            competition_id=competition.id,
            nba_stats_game_id="1522400001",
            status=SummerLeagueGameStatus.FINAL,
            tip_datetime=datetime(2024, 7, 12, 20, 0),
            home_nba_stats_team_id="1610612753",
            away_nba_stats_team_id="1610612739",
            home_score=118,
            away_score=101,
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
    assert game.home_score == 118
    assert game.away_score == 101


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
        select(func.count()).select_from(SummerLeagueEdition)
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
    competition = SummerLeagueEdition(
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

    competition = SummerLeagueEdition(
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
            select(SummerLeagueEdition).where(
                SummerLeagueEdition.year == 2024,  # type: ignore[arg-type]
                SummerLeagueEdition.league_id == "15",  # type: ignore[arg-type]
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
    competition = SummerLeagueEdition(
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
    competition = SummerLeagueEdition(
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
    competition = SummerLeagueEdition(
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


def _shot_result_set(rows: list[list[object]]) -> dict[str, object]:
    headers = [
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
    return _result_set("Shot_Chart_Detail", headers, rows)


def _shot_row(
    *,
    game_id: str,
    event_id: int,
    player_id: int,
    player_name: str,
    team_id: int,
    made: int,
) -> list[object]:
    return [
        "Shot Chart Detail",
        game_id,
        event_id,
        player_id,
        player_name,
        team_id,
        "Team",
        2,
        6,
        12,
        "Made Shot" if made else "Missed Shot",
        "Jump Shot",
        "2PT Field Goal",
        "Mid-Range",
        "Center(C)",
        "16-24 ft.",
        18,
        5,
        190,
        1,
        made,
        "2024-07-12",
        "ORL",
        "CLE",
    ]


def _pbp_result_set(rows: list[list[object]]) -> dict[str, object]:
    headers = [
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
    return _result_set("PlayByPlay", headers, rows)


def _pbp_row(
    *,
    game_id: str,
    event_num: int,
    person1_id: object,
    person2_id: object = None,
) -> list[object]:
    return [
        game_id,
        event_num,
        1,
        2,
        "7:00 PM",
        "6:12",
        "Play description",
        None,
        None,
        "4 - 2",
        "2",
        4,
        person1_id or 0,
        "Player",
        1610612753,
        "Orlando",
        "Magic",
        "ORL",
        (0 if not person2_id else 5),
        person2_id or 0,
        "Player" if person2_id else None,
        1610612739 if person2_id else None,
        "Cleveland" if person2_id else None,
        "Cavaliers" if person2_id else None,
        "CLE" if person2_id else None,
        0,
        0,
        None,
        None,
        None,
        None,
        None,
        0,
    ]


def _write_two_game_shot_pbp_fixture(raw_root: Path) -> None:
    """Write a two-game raw fixture stressing the bulk shot/PBP batch identity path.

    Game 1 carries two shots for a pre-resolved player (``2001``, seen
    twice -- exercises last-row-wins identity dedup within a batch) and one
    shot for a genuinely unresolved player (``2003``, never pre-seeded).
    Game 2 carries one more shot for the *same* resolved player ``2001``
    (exercises identity reuse across games in a single multi-game call)
    plus PBP events referencing: ``2001`` again (person1, resolved),
    a wholly unknown actor ``2099`` (person1, stays unresolved, no source
    player ever minted for it), and ``2002`` (person1 in game 2, pre-resolved
    but never appearing in any shot -- proves PBP resolution reuses an
    existing source player without ever creating one) paired with ``2001``
    again (person2, resolved).
    """
    run_dir = raw_root / "2024" / "15"
    game_ids = ["1522400101", "1522400102"]
    for game_id in game_ids:
        (run_dir / "games" / game_id).mkdir(parents=True)

    run_dir.joinpath("manifest.json").write_text(
        json.dumps(
            {
                "year": 2024,
                "league_id": "15",
                "venue": "las_vegas",
                "team_gamelog_rows": 4,
                "player_gamelog_rows": 0,
                "game_ids": game_ids,
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
                                game_ids[0],
                                "2024-07-12",
                                "ORL vs. CLE",
                                106,
                            ],
                            [
                                1610612739,
                                "CLE",
                                "Cleveland Cavaliers",
                                game_ids[0],
                                "2024-07-12",
                                "CLE @ ORL",
                                79,
                            ],
                            [
                                1610612753,
                                "ORL",
                                "Orlando Magic",
                                game_ids[1],
                                "2024-07-13",
                                "ORL vs. CLE",
                                100,
                            ],
                            [
                                1610612739,
                                "CLE",
                                "Cleveland Cavaliers",
                                game_ids[1],
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

    for game_id in game_ids:
        game_dir = run_dir / "games" / game_id
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

    game_dir_1 = run_dir / "games" / game_ids[0]
    game_dir_2 = run_dir / "games" / game_ids[1]

    game_dir_1.joinpath("shotchartdetail.json").write_text(
        json.dumps(
            {
                "resultSets": [
                    _shot_result_set(
                        [
                            _shot_row(
                                game_id=game_ids[0],
                                event_id=1,
                                player_id=2001,
                                player_name="Resolved Guard",
                                team_id=1610612753,
                                made=1,
                            ),
                            _shot_row(
                                game_id=game_ids[0],
                                event_id=2,
                                player_id=2001,
                                player_name="Resolved Guard",
                                team_id=1610612753,
                                made=0,
                            ),
                            _shot_row(
                                game_id=game_ids[0],
                                event_id=3,
                                player_id=2003,
                                player_name="Unresolved Forward",
                                team_id=1610612739,
                                made=1,
                            ),
                        ]
                    )
                ]
            }
        )
    )
    game_dir_2.joinpath("shotchartdetail.json").write_text(
        json.dumps(
            {
                "resultSets": [
                    _shot_result_set(
                        [
                            _shot_row(
                                game_id=game_ids[1],
                                event_id=1,
                                player_id=2001,
                                player_name="Resolved Guard",
                                team_id=1610612753,
                                made=1,
                            ),
                        ]
                    )
                ]
            }
        )
    )

    game_dir_1.joinpath("playbyplayv2.json").write_text(
        json.dumps(
            {
                "resultSets": [
                    _pbp_result_set(
                        [
                            _pbp_row(game_id=game_ids[0], event_num=1, person1_id=2001),
                            _pbp_row(game_id=game_ids[0], event_num=2, person1_id=2099),
                        ]
                    )
                ]
            }
        )
    )
    game_dir_2.joinpath("playbyplayv2.json").write_text(
        json.dumps(
            {
                "resultSets": [
                    _pbp_result_set(
                        [
                            _pbp_row(
                                game_id=game_ids[1],
                                event_num=1,
                                person1_id=2002,
                                person2_id=2001,
                            ),
                        ]
                    )
                ]
            }
        )
    )


@pytest.mark.asyncio
async def test_bulk_shot_and_pbp_normalization_matches_row_by_row_semantics(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """Golden-row check: the bulk-write rewrite reproduces exact per-event semantics (#627).

    Encodes the same contract the pre-#627 row-by-row implementation
    guaranteed (read directly off ``_upsert_source_player``,
    ``_upsert_shot_event``, ``_resolve_actor_id``, and ``_upsert_pbp_event``
    before they were replaced): idempotent upsert keyed on
    (game, event) pairs, last-row-wins identity dedup within one call,
    identity reuse across every game in a multi-game batch, an
    already-resolved source player's ``canonical_player_id`` carried onto
    both shot and PBP rows untouched, and an unresolved actor/player
    producing ``NULL`` FKs rather than a guess -- all via a single call
    across both games (``game_ids=None``), proving the batched identity
    preload and chunked ``INSERT ... ON CONFLICT`` writes span a multi-game
    call correctly.
    """
    _write_two_game_shot_pbp_fixture(tmp_path)
    await audit_summer_league_raw(
        db_session, raw_root=tmp_path, year=2024, league_id="15"
    )
    await normalize_competition_games(
        db_session, year=2024, league_id="15", raw_root=tmp_path
    )
    await db_session.flush()

    # Pre-resolve player 2001 (as an earlier resolution pass would have)
    # and player 2002 (referenced only via PBP's person1 in game 2, must
    # resolve there without ever appearing in a shot for that game).
    resolved_guard = PlayerMaster(display_name="Resolved Guard", slug="resolved-guard")
    resolved_wing = PlayerMaster(display_name="Resolved Wing", slug="resolved-wing")
    db_session.add_all([resolved_guard, resolved_wing])
    await db_session.flush()
    assert resolved_guard.id is not None
    assert resolved_wing.id is not None
    db_session.add_all(
        [
            SummerLeagueSourceRecord(
                nba_stats_person_id="2001",
                raw_player_name="Resolved Guard",
                normalized_name=_normalized_name_key("Resolved Guard"),
                first_seen_year=2023,
                last_seen_year=2023,
                canonical_player_id=resolved_guard.id,
                resolution_status=SummerLeagueResolutionStatus.EXTERNAL_ID,
                resolution_confidence=1.0,
                resolved_by="test",
            ),
            SummerLeagueSourceRecord(
                nba_stats_person_id="2002",
                raw_player_name="Resolved Wing",
                normalized_name=_normalized_name_key("Resolved Wing"),
                first_seen_year=2023,
                last_seen_year=2023,
                canonical_player_id=resolved_wing.id,
                resolution_status=SummerLeagueResolutionStatus.EXTERNAL_ID,
                resolution_confidence=1.0,
                resolved_by="test",
            ),
        ]
    )
    await db_session.flush()

    shot_report = await normalize_shot_events(
        db_session, year=2024, league_id="15", raw_root=tmp_path
    )
    pbp_report = await normalize_pbp_events(
        db_session, year=2024, league_id="15", raw_root=tmp_path
    )

    # -- Shot events: 3 in game 1 (idempotent upsert on repeated person 2001,
    # not merged into one row -- each event_id is its own row) + 1 in game 2.
    assert shot_report.games_processed == 2
    assert shot_report.games_with_shots == 2
    assert shot_report.shot_events_upserted == 4

    shots = (
        (
            await db_session.execute(
                select(SummerLeagueShotEvent).order_by(
                    SummerLeagueShotEvent.nba_stats_game_id,
                    SummerLeagueShotEvent.nba_stats_game_event_id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(shots) == 4
    assert [s.nba_stats_game_event_id for s in shots] == [1, 2, 3, 1]

    # Both game-1 events for the pre-resolved player carry its canonical id
    # and made/unmade preserved distinctly per event.
    assert shots[0].nba_stats_person_id == "2001"
    assert shots[0].player_id == resolved_guard.id
    assert shots[0].made is True
    assert shots[1].nba_stats_person_id == "2001"
    assert shots[1].player_id == resolved_guard.id
    assert shots[1].made is False
    # The genuinely unresolved player (never pre-seeded) gets a new UNRESOLVED
    # source player but a NULL canonical link on its shot row.
    assert shots[2].nba_stats_person_id == "2003"
    assert shots[2].player_id is None
    # Game 2's shot reuses the SAME source_player_id as game 1's, proving
    # identity resolution was shared across the whole multi-game batch.
    assert shots[3].nba_stats_game_id == "1522400102"
    assert shots[3].source_player_id == shots[0].source_player_id
    assert shots[3].player_id == resolved_guard.id

    # -- Source players: exactly two distinct identities were touched by
    # shots (2001, 2003); resolution fields were left untouched by the bulk
    # upsert for the already-resolved id.
    source_2001 = (
        await db_session.execute(
            select(SummerLeagueSourceRecord).where(
                SummerLeagueSourceRecord.nba_stats_person_id == "2001"  # type: ignore[arg-type]
            )
        )
    ).scalar_one()
    assert source_2001.canonical_player_id == resolved_guard.id
    assert source_2001.resolution_status == SummerLeagueResolutionStatus.EXTERNAL_ID
    assert source_2001.first_seen_year == 2023  # unchanged: LEAST(2023, 2024)
    assert source_2001.last_seen_year == 2024  # extended: GREATEST(2023, 2024)

    source_2003 = (
        await db_session.execute(
            select(SummerLeagueSourceRecord).where(
                SummerLeagueSourceRecord.nba_stats_person_id == "2003"  # type: ignore[arg-type]
            )
        )
    ).scalar_one()
    assert source_2003.raw_player_name == "Unresolved Forward"
    assert source_2003.canonical_player_id is None
    assert source_2003.resolution_status == SummerLeagueResolutionStatus.UNRESOLVED
    assert source_2003.first_seen_year == 2024
    assert source_2003.last_seen_year == 2024

    # 2002 was pre-seeded (resolved) but never appears in any shot -- the
    # shot-side bulk upsert must never touch it.
    source_2002 = (
        await db_session.execute(
            select(SummerLeagueSourceRecord).where(
                SummerLeagueSourceRecord.nba_stats_person_id == "2002"  # type: ignore[arg-type]
            )
        )
    ).scalar_one()
    assert source_2002.raw_player_name == "Resolved Wing"
    assert source_2002.canonical_player_id == resolved_wing.id
    assert source_2002.first_seen_year == 2023  # untouched by the PBP-only pass

    # -- PBP events: 2 in game 1, 1 in game 2.
    assert pbp_report.games_processed == 2
    assert pbp_report.games_with_pbp == 2
    assert pbp_report.pbp_events_upserted == 3

    pbp_events = (
        (
            await db_session.execute(
                select(SummerLeaguePlayByPlayEvent).order_by(
                    SummerLeaguePlayByPlayEvent.nba_stats_game_id,
                    SummerLeaguePlayByPlayEvent.event_num,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(pbp_events) == 3

    # Game 1, event 1: person1 = 2001, already resolved -> person1_id set.
    assert pbp_events[0].nba_stats_game_id == "1522400101"
    assert pbp_events[0].person1_nba_id == "2001"
    assert pbp_events[0].person1_id == resolved_guard.id
    # Game 1, event 2: person1 = 2099, never seen anywhere -> stays NULL,
    # and _resolve_actor_id-equivalent lookup never creates a source player.
    assert pbp_events[1].person1_nba_id == "2099"
    assert pbp_events[1].person1_id is None
    unknown_actor = (
        await db_session.execute(
            select(SummerLeagueSourceRecord).where(
                SummerLeagueSourceRecord.nba_stats_person_id == "2099"  # type: ignore[arg-type]
            )
        )
    ).scalar_one_or_none()
    assert unknown_actor is None

    # Game 2, event 1: person1 = 2002 (resolved from the pre-seed, despite
    # never appearing in any shot), person2 = 2001 (resolved from the
    # pre-seed) -- both FKs set.
    assert pbp_events[2].nba_stats_game_id == "1522400102"
    assert pbp_events[2].person1_nba_id == "2002"
    assert pbp_events[2].person1_id == resolved_wing.id
    assert pbp_events[2].person2_nba_id == "2001"
    assert pbp_events[2].person2_id == resolved_guard.id
    assert pbp_events[2].person3_id is None

    # Idempotency: a second call over the same fixture leaves row counts and
    # values unchanged (updates in place via ON CONFLICT, no duplicates).
    shot_report_2 = await normalize_shot_events(
        db_session, year=2024, league_id="15", raw_root=tmp_path
    )
    pbp_report_2 = await normalize_pbp_events(
        db_session, year=2024, league_id="15", raw_root=tmp_path
    )
    assert shot_report_2.shot_events_upserted == 4
    assert pbp_report_2.pbp_events_upserted == 3
    shot_count = await db_session.scalar(
        select(func.count()).select_from(SummerLeagueShotEvent)
    )
    pbp_count = await db_session.scalar(
        select(func.count()).select_from(SummerLeaguePlayByPlayEvent)
    )
    assert shot_count == 4
    assert pbp_count == 3
