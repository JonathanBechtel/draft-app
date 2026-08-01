"""Normalize audited Summer League raw data into product tables."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.nba_teams import NbaTeam as _NbaTeam  # noqa: F401
from app.schemas.player_affiliation import (
    AffiliationStatus,
    AffiliationType,
    PlayerAffiliation,
)
from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeagueDataQuality,
    SummerLeagueGame,
    SummerLeagueGameStatus,
    SummerLeaguePlayByPlayEvent,
    SummerLeaguePlayerGameLog,
    SummerLeagueParticipation,
    SummerLeagueRawFile,
    SummerLeagueRawFileStatus,
    SummerLeagueRawRun,
    SummerLeagueRawRunStatus,
    SummerLeagueResolutionStatus,
    SummerLeagueShotEvent,
    SummerLeagueSourcePlayer,
    SummerLeagueTeamEntry,
    SummerLeagueTeamGameLog,
)
from app.services.player_mention_service import _normalized_name_key
from app.services.summer_league.nba_stats_client import (
    NBAStatsResultSet,
    extract_result_sets,
)
from app.services.summer_league.metrics import MIN_COMPLETE_TEAM_MP


# stats.nba.com occasionally corrupts boxscoretraditionalv2 minutes for a
# single game (observed on SL game 1322600006: every starter's MIN came back
# ~+97:00 — e.g. '129:53' for a 32:53 stint — with team MIN inflated to
# '705:00' to match, while all other columns stayed correct).
# boxscoreadvancedv2 carried the true minutes for the same game, so the merge
# step falls back to the advanced box whenever the traditional value is
# implausible. Ceilings sit far above any legitimate Summer League value
# (players top out well under an hour even with overtimes; a team's five
# positions under 300 total minutes).
MAX_PLAUSIBLE_PLAYER_SECONDS = 70 * 60
MAX_PLAUSIBLE_TEAM_MINUTES = 350

# Row count per chunked INSERT ... ON CONFLICT statement issued by the bulk
# shot/PBP upsert helpers below (#627). Batches are already bounded to
# EVENT_BATCH_SIZE=8 games by the ingest runner (#625), so a single chunk
# normally covers a whole call; chunking is kept for the explicit
# full-reconciliation path (``game_ids=None``), which can still process a
# whole venue -- and its ~10k shot events -- in one call.
BULK_UPSERT_CHUNK_SIZE = 500


@dataclass(frozen=True)
class ParsedTeamGamelogRow:
    """Parsed source team gamelog row."""

    game_id: str
    game_date: date | None
    nba_stats_team_id: str
    raw_team_name: str
    raw_team_abbreviation: str | None
    matchup: str | None
    pts: int | None


@dataclass(frozen=True)
class ParsedTeamBoxRow:
    """Parsed team box-score row with traditional and optional advanced stats."""

    game_id: str
    nba_stats_team_id: str
    raw_team_name: str
    raw_team_abbreviation: str | None
    minutes: int | None = None
    pts: int | None = None
    fgm: int | None = None
    fga: int | None = None
    fg_pct: float | None = None
    fg3m: int | None = None
    fg3a: int | None = None
    fg3_pct: float | None = None
    ftm: int | None = None
    fta: int | None = None
    ft_pct: float | None = None
    oreb: int | None = None
    dreb: int | None = None
    reb: int | None = None
    ast: int | None = None
    stl: int | None = None
    blk: int | None = None
    tov: int | None = None
    pf: int | None = None
    plus_minus: int | None = None
    off_rating: float | None = None
    def_rating: float | None = None
    net_rating: float | None = None
    ast_pct: float | None = None
    reb_pct: float | None = None
    efg_pct: float | None = None
    ts_pct: float | None = None
    pace: float | None = None


@dataclass(frozen=True)
class ParsedPlayerGamelogRow:
    """Parsed source player identity from the season player gamelog."""

    nba_stats_person_id: str
    raw_player_name: str
    nba_stats_team_id: str | None = None


@dataclass(frozen=True)
class ParsedPlayerBoxRow:
    """Parsed player box-score row with optional advanced/scoring stats."""

    game_id: str
    nba_stats_person_id: str
    raw_player_name: str
    nba_stats_team_id: str
    starter_position: str | None = None
    comment: str | None = None
    minutes_seconds: int | None = None
    pts: int | None = None
    fgm: int | None = None
    fga: int | None = None
    fg_pct: float | None = None
    fg3m: int | None = None
    fg3a: int | None = None
    fg3_pct: float | None = None
    ftm: int | None = None
    fta: int | None = None
    ft_pct: float | None = None
    oreb: int | None = None
    dreb: int | None = None
    reb: int | None = None
    ast: int | None = None
    stl: int | None = None
    blk: int | None = None
    tov: int | None = None
    pf: int | None = None
    plus_minus: int | None = None
    off_rating: float | None = None
    def_rating: float | None = None
    net_rating: float | None = None
    ast_pct: float | None = None
    oreb_pct: float | None = None
    dreb_pct: float | None = None
    reb_pct: float | None = None
    tm_tov_pct: float | None = None
    efg_pct: float | None = None
    ts_pct: float | None = None
    usg_pct: float | None = None
    pace: float | None = None
    pie: float | None = None
    pct_fga_2pt: float | None = None
    pct_fga_3pt: float | None = None
    pct_pts_2pt: float | None = None
    pct_pts_3pt: float | None = None
    pct_pts_ft: float | None = None


@dataclass(frozen=True)
class ParsedShotEvent:
    """Parsed shot attempt row from a shotchartdetail JSON snapshot."""

    nba_stats_game_id: str
    nba_stats_game_event_id: int
    nba_stats_person_id: str
    raw_player_name: str
    nba_stats_team_id: str
    period: int | None
    minutes_remaining: int | None
    seconds_remaining: int | None
    loc_x: int | None
    loc_y: int | None
    shot_distance: int | None
    shot_type: str | None
    shot_zone_basic: str | None
    shot_zone_area: str | None
    shot_zone_range: str | None
    action_type: str | None
    made: bool


@dataclass(frozen=True)
class ParsedPBPEvent:
    """Parsed play-by-play event row from a playbyplayv2 JSON snapshot."""

    nba_stats_game_id: str
    event_num: int
    period: int | None
    clock: str | None
    event_msg_type: int | None
    home_score: int | None
    away_score: int | None
    score_margin: int | None
    person1_nba_id: str | None
    person2_nba_id: str | None
    person3_nba_id: str | None
    description: str | None


@dataclass(frozen=True)
class _SourcePlayerRef:
    """Lightweight identity + resolution snapshot for one bulk-upserted source player.

    Returned by :func:`_bulk_upsert_source_players` instead of a full
    ``SummerLeagueSourcePlayer`` ORM instance -- the shot-event row builder
    only ever needs the row's ``id`` (FK target) and ``canonical_player_id``
    (copied onto the shot's ``player_id``).
    """

    id: int
    canonical_player_id: int | None


@dataclass(frozen=True)
class SummerLeagueShotEventReport:
    """Counts from one Summer League shot event normalization run."""

    year: int
    league_id: str
    competition_id: int
    shot_events_upserted: int
    games_processed: int
    games_with_shots: int


@dataclass(frozen=True)
class SummerLeaguePBPEventReport:
    """Counts from one Summer League PBP event normalization run."""

    year: int
    league_id: str
    competition_id: int
    pbp_events_upserted: int
    games_processed: int
    games_with_pbp: int


@dataclass(frozen=True)
class SummerLeagueNormalizationReport:
    """Counts from one Summer League competition/team/game normalization run."""

    year: int
    league_id: str
    competition_id: int
    teams_upserted: int
    games_upserted: int
    team_game_logs_upserted: int
    data_quality: SummerLeagueDataQuality


@dataclass(frozen=True)
class SummerLeaguePlayerLogReport:
    """Counts from one Summer League source-player/player-log normalization run."""

    year: int
    league_id: str
    competition_id: int
    source_players_upserted: int
    player_game_logs_upserted: int
    player_game_logs_skipped: int


async def normalize_competition_games(
    db: AsyncSession,
    *,
    year: int,
    league_id: str,
    raw_root: Path,
    limit_games: int | None = None,
) -> SummerLeagueNormalizationReport:
    """Normalize competition, teams, games, and team game logs for one slice."""
    raw_run = await _get_raw_run(db, year=year, league_id=league_id)
    if raw_run.id is None:
        raise RuntimeError("Raw run id was not populated")
    raw_files = await _get_raw_files(db, raw_run_id=raw_run.id)
    quality = _competition_quality(raw_run, raw_files)
    # Whole-slice completion evidence (#530): only a fully audited raw run --
    # every expected file for this year/league successfully captured -- is
    # strong enough evidence to establish Final for a game normalization is
    # seeing for the first time with no scoreboard-tracked status at all (see
    # `resolve_game_status`). A live event's raw run stays PARTIAL for as
    # long as any game hasn't finished, so this never fires mid-event.
    raw_run_complete = raw_run.status == SummerLeagueRawRunStatus.COMPLETE
    competition = await _upsert_competition(db, raw_run, quality)
    await db.flush()
    if competition.id is None:
        raise RuntimeError("Competition id was not populated after flush")

    limited_game_ids = _limited_game_ids(
        raw_root=raw_root,
        year=year,
        league_id=league_id,
        limit_games=limit_games,
    )
    team_gamelog_rows = _filter_team_gamelog_rows(
        parse_team_gamelog(raw_root / f"{year}/{league_id}/leaguegamelog_team.json"),
        limited_game_ids,
    )
    # Seed both maps from the competition's full canonical set (#633) -- not
    # just this batch's season-gamelog rows -- so a game/team already created
    # by scoreboard ingest (which runs ahead of normalization every tick, see
    # ``scoreboard_ingest.upsert_scoreboard_games``) is still found below even
    # on a run where the season-wide LeagueGameLog feed hasn't caught up to
    # that particular game yet.
    teams_by_source_id = await _teams_by_source_id(db, competition.id)
    games_by_source_id = await _games_by_source_id(db, competition.id)
    for row in team_gamelog_rows:
        team = await _upsert_team_entry(db, competition.id, row)
        await db.flush()
        teams_by_source_id[row.nba_stats_team_id] = team

    game_rows = _group_game_rows(team_gamelog_rows)
    for game_id, rows in game_rows.items():
        game = await _upsert_game(
            db,
            competition.id,
            game_id,
            rows,
            teams_by_source_id,
            quality,
            raw_run_complete=raw_run_complete,
        )
        await db.flush()
        games_by_source_id[game_id] = game

    team_log_count = 0
    team_log_keys: set[tuple[str, str]] = set()
    for box_row in parse_team_box_rows(
        raw_root / f"{year}/{league_id}",
        game_ids=limited_game_ids,
    ):
        box_game = games_by_source_id.get(box_row.game_id)
        box_team = teams_by_source_id.get(box_row.nba_stats_team_id)
        if (
            box_game is None
            or box_team is None
            or box_game.id is None
            or box_team.id is None
        ):
            continue
        await _upsert_team_game_log(
            db, competition.id, box_game.id, box_team.id, box_row
        )
        team_log_keys.add((box_row.game_id, box_row.nba_stats_team_id))
        team_log_count += 1
        # Live-score fallback (#633): the season LeagueGameLog can lag well
        # behind the per-game TeamStats endpoints while a game is in
        # progress, leaving a scheduled, passed-tip game with null scores
        # despite real box data already sitting on disk. A game LeagueGameLog
        # *did* cover this batch already got the authoritative season value
        # from ``_upsert_game`` above and must not be touched here. Matched
        # against the game's raw provider team ids (set by scoreboard ingest
        # independently of local team-entry resolution) rather than
        # ``box_team`` -- no ``MATCHUP`` string exists on a box row to
        # otherwise tell home from away (contrast :func:`_home_row`). Only
        # fills a currently-null score -- never overwrites one already
        # present (e.g. scoreboard ingest's own live read of
        # ``scheduleleaguev2``, or an earlier pass here) with this on-disk
        # per-game snapshot, which the full-ingestion path's ``force=False``
        # can leave stale well behind the game's actual current state.
        if box_row.game_id not in game_rows and box_row.pts is not None:
            if (
                box_row.nba_stats_team_id == box_game.home_nba_stats_team_id
                and box_game.home_score is None
            ):
                box_game.home_score = box_row.pts
                box_game.updated_at = _utc_now_naive()
            elif (
                box_row.nba_stats_team_id == box_game.away_nba_stats_team_id
                and box_game.away_score is None
            ):
                box_game.away_score = box_row.pts
                box_game.updated_at = _utc_now_naive()

    for gamelog_row in team_gamelog_rows:
        key = (gamelog_row.game_id, gamelog_row.nba_stats_team_id)
        if key in team_log_keys:
            continue
        fallback_game = games_by_source_id.get(gamelog_row.game_id)
        fallback_team = teams_by_source_id.get(gamelog_row.nba_stats_team_id)
        if (
            fallback_game is None
            or fallback_team is None
            or fallback_game.id is None
            or fallback_team.id is None
        ):
            continue
        await _upsert_team_game_log(
            db,
            competition.id,
            fallback_game.id,
            fallback_team.id,
            _team_box_row_from_gamelog(gamelog_row),
            source_endpoint="leaguegamelog_team",
        )
        team_log_count += 1

    await db.flush()
    await refresh_competition_date_window(db, competition_id=competition.id)
    await db.flush()
    return SummerLeagueNormalizationReport(
        year=year,
        league_id=league_id,
        competition_id=competition.id,
        teams_upserted=len(teams_by_source_id),
        games_upserted=len(games_by_source_id),
        team_game_logs_upserted=team_log_count,
        data_quality=quality,
    )


async def find_incomplete_team_box_game_ids(
    db: AsyncSession, *, competition_id: int
) -> list[str]:
    """Return source game IDs whose team boxes fail the metrics completeness gate.

    :func:`normalize_competition_games` falls back to a
    :class:`~app.schemas.summer_league.SummerLeagueTeamGameLog` row built from
    the season gamelog (``source_endpoint="leaguegamelog_team"``) whenever a
    game's per-game ``boxscoretraditionalv2``/``boxscoreadvancedv2`` files
    weren't on disk at normalize time. That fallback never carries team
    ``minutes`` -- the season gamelog doesn't report it -- which permanently
    blocks the competition's ``adv_eligible`` gate (see
    ``app.services.summer_league.metrics``) even once real box scores exist.
    An early official box can also contain fewer than two team rows or blank /
    partial minutes, so this uses the same two-rows-at-150-minutes predicate as
    the metrics completeness calculation rather than trusting the endpoint name.
    Games with no team rows remain retryable once the scoreboard marks them
    in-progress or final. Zero-row scheduled games are excluded because they
    are normally future games that have not produced any box data to repair yet.

    The raw ingestor treats an on-disk per-game file as permanent
    (``force=False`` skips anything already written), so a snapshot fetched
    moments too early -- before NBA Stats finished posting the official box
    for a just-finished game -- silently freezes that game on the fallback
    forever with no other retry path. Callers (see
    ``app.cli.summer_league_ingest_runner``) use this helper to force a
    fresh per-game refetch for exactly the still-incomplete games, so the
    gap self-heals on the next ingest run instead of persisting.

    Args:
        db: Async database session.
        competition_id: The competition to scope the check to.

    Returns:
        Distinct ``nba_stats_game_id`` values for started games that do not have
        exactly two team rows of at least ``MIN_COMPLETE_TEAM_MP`` minutes each.
        Empty when every started game passes the advanced-metrics completeness
        predicate.
    """
    stmt = (
        select(SummerLeagueGame.nba_stats_game_id)  # type: ignore[call-overload]
        .outerjoin(
            SummerLeagueTeamGameLog,
            SummerLeagueTeamGameLog.game_id == SummerLeagueGame.id,  # type: ignore[arg-type]
        )
        .where(SummerLeagueGame.competition_id == competition_id)  # type: ignore[arg-type]
        .group_by(SummerLeagueGame.id, SummerLeagueGame.nba_stats_game_id)
        .having(
            and_(
                or_(
                    func.count(SummerLeagueTeamGameLog.id)  # type: ignore[arg-type]
                    != 2,
                    func.min(func.coalesce(SummerLeagueTeamGameLog.minutes, 0))
                    < MIN_COMPLETE_TEAM_MP,
                ),
                or_(
                    func.count(SummerLeagueTeamGameLog.id)  # type: ignore[arg-type]
                    > 0,
                    SummerLeagueGame.status.in_(  # type: ignore[attr-defined]
                        (
                            SummerLeagueGameStatus.IN_PROGRESS,
                            SummerLeagueGameStatus.FINAL,
                        )
                    ),
                ),
            )
        )
        .order_by(SummerLeagueGame.nba_stats_game_id)
    )
    result = await db.execute(stmt)
    return [row[0] for row in result.all() if row[0]]


async def refresh_competition_date_window(
    db: AsyncSession, *, competition_id: int
) -> None:
    """Recompute one competition's ``starts_on``/``ends_on`` from its games.

    Recomputes from the *full* current ``summer_league_games`` set for
    ``competition_id`` every time (rather than only widening), so the window
    self-corrects as games are added, corrected, or rescheduled -- both the
    normalize path (:func:`normalize_competition_games`) and the
    scoreboard/schedule path
    (:func:`app.services.summer_league.scoreboard_ingest.upsert_scoreboard_games`)
    write games, so both call this after they do. Rows with a null
    ``game_date`` are excluded from the aggregate. This is what unblocks the
    Event Desk's opening-morning bootstrap (#527's
    ``_needs_scoreboard_bootstrap`` / ``_synthetic_calendar_dates`` in
    ``app/cli/sl_desk_tick.py``), which derives its synthetic event window
    from these two fields and was previously permanently inert because
    nothing ever populated them.

    Args:
        db: Active database session (caller controls the transaction/commit;
            this issues one bounded ``SELECT`` + attribute assignment and
            never commits).
        competition_id: The competition whose game dates should be
            aggregated.

    Note:
        A competition with zero dated games is left untouched -- both fields
        stay whatever they already were (typically null) rather than being
        cleared, since an empty aggregate carries no information about the
        event's actual window.
    """
    result = await db.execute(
        select(
            func.min(SummerLeagueGame.game_date),
            func.max(SummerLeagueGame.game_date),
        ).where(
            SummerLeagueGame.competition_id == competition_id,  # type: ignore[arg-type]
            SummerLeagueGame.game_date.is_not(None),  # type: ignore[union-attr]
        )
    )
    min_date, max_date = result.one()
    if min_date is None or max_date is None:
        return
    competition = await db.get(SummerLeagueCompetition, competition_id)
    if competition is None:
        return
    competition.starts_on = min_date
    competition.ends_on = max_date
    competition.updated_at = _utc_now_naive()


async def normalize_player_game_logs(
    db: AsyncSession,
    *,
    year: int,
    league_id: str,
    raw_root: Path,
    limit_games: int | None = None,
) -> SummerLeaguePlayerLogReport:
    """Normalize source players and player game logs for one Summer League slice."""
    competition = await _get_competition(db, year=year, league_id=league_id)
    if competition.id is None:
        raise RuntimeError("Competition id was not populated")

    season_dir = raw_root / f"{year}/{league_id}"
    limited_game_ids = _limited_game_ids(
        raw_root=raw_root,
        year=year,
        league_id=league_id,
        limit_games=limit_games,
    )
    source_player_ids: set[str] = set()
    if limited_game_ids is None:
        for gamelog_row in parse_player_gamelog(
            season_dir / "leaguegamelog_player.json"
        ):
            await _upsert_source_player(db, gamelog_row, year=year)
            source_player_ids.add(gamelog_row.nba_stats_person_id)

    await db.flush()
    competition_id = competition.id
    games_by_source_id = await _games_by_source_id(db, competition_id)
    teams_by_source_id = await _teams_by_source_id(db, competition_id)
    participations = await _participations_by_key(db, competition_id)
    recorded_at = _utc_now_naive()

    upserted_logs = 0
    skipped_logs = 0
    # Player-games (source game / person / team ids) the season-log fallback must
    # not touch: the ones written this run from per-game boxscores, plus any
    # already persisted for this competition. Seeding with existing rows means the
    # fallback only inserts genuinely-missing player-games and never downgrades an
    # existing (possibly richer, per-game-sourced) line to the traditional-only
    # season line — even if the per-game snapshots are absent on this run.
    covered: set[tuple[str, str, str]] = set()
    existing_keys = await db.execute(
        select(  # type: ignore[call-overload]
            SummerLeagueGame.nba_stats_game_id,
            SummerLeagueSourcePlayer.nba_stats_person_id,
            SummerLeagueTeamEntry.nba_stats_team_id,
        )
        .select_from(SummerLeaguePlayerGameLog)
        .join(
            SummerLeagueGame,
            SummerLeagueGame.id == SummerLeaguePlayerGameLog.game_id,  # type: ignore[arg-type]
        )
        .join(
            SummerLeagueSourcePlayer,
            SummerLeagueSourcePlayer.id  # type: ignore[arg-type]
            == SummerLeaguePlayerGameLog.source_player_id,
        )
        .join(
            SummerLeagueTeamEntry,
            SummerLeagueTeamEntry.id  # type: ignore[arg-type]
            == SummerLeaguePlayerGameLog.team_entry_id,
        )
        .where(SummerLeaguePlayerGameLog.competition_id == competition_id)  # type: ignore[arg-type]
    )
    for game_key, person_key, team_key in existing_keys.all():
        covered.add((game_key, person_key, team_key))

    async def _process_box_row(box_row: ParsedPlayerBoxRow) -> None:
        nonlocal upserted_logs, skipped_logs
        source_player = await _upsert_source_player(
            db,
            ParsedPlayerGamelogRow(
                nba_stats_person_id=box_row.nba_stats_person_id,
                raw_player_name=box_row.raw_player_name,
                nba_stats_team_id=box_row.nba_stats_team_id,
            ),
            year=year,
        )
        source_player_ids.add(box_row.nba_stats_person_id)
        await db.flush()

        game = games_by_source_id.get(box_row.game_id)
        team = teams_by_source_id.get(box_row.nba_stats_team_id)
        if (
            game is None
            or team is None
            or game.id is None
            or team.id is None
            or source_player.id is None
        ):
            skipped_logs += 1
            return

        await _upsert_player_game_log(
            db,
            competition_id,
            game.id,
            team.id,
            source_player,
            box_row,
            participations=participations,
            recorded_at=recorded_at,
        )
        covered.add(
            (box_row.game_id, box_row.nba_stats_person_id, box_row.nba_stats_team_id)
        )
        upserted_logs += 1

    for box_row in parse_player_box_rows(season_dir, game_ids=limited_game_ids):
        await _process_box_row(box_row)

    # Season-log fallback: pre-2017 years have no per-game boxscore data at
    # stats.nba.com, but the season LeagueGameLog carries a full traditional line.
    # Fill any player-game the per-game pass didn't already cover. (Skipped for
    # limited-game runs, which target specific per-game files.)
    if limited_game_ids is None:
        for box_row in parse_player_gamelog_box_rows(
            season_dir / "leaguegamelog_player.json"
        ):
            key = (
                box_row.game_id,
                box_row.nba_stats_person_id,
                box_row.nba_stats_team_id,
            )
            if key in covered:
                continue
            await _process_box_row(box_row)

    await db.flush()
    return SummerLeaguePlayerLogReport(
        year=year,
        league_id=league_id,
        competition_id=competition.id,
        source_players_upserted=len(source_player_ids),
        player_game_logs_upserted=upserted_logs,
        player_game_logs_skipped=skipped_logs,
    )


def _raise_availability_flag(
    competition: SummerLeagueCompetition,
    attr: str,
    *,
    has_events: bool,
    game_ids: set[str] | None,
) -> None:
    """Set availability from actual parsed rows, not file presence.

    Shared by :func:`normalize_shot_events`/:func:`normalize_pbp_events`. A
    batch call (``game_ids`` set) only ever raises the flag -- it must never
    downgrade it back to ``False`` just because *this batch's* games
    happened to have no events while an earlier, already-committed batch
    did. Only a whole-venue call (``game_ids=None``, matching prior behavior
    exactly) may set it back to ``False``.

    Args:
        competition: The competition row to update in place.
        attr: ``"shotchart_available"`` or ``"pbp_available"``.
        has_events: Whether this call's batch had at least one parsed event.
        game_ids: The batch's game-id filter, or ``None`` for a whole-venue
            call.
    """
    if has_events:
        setattr(competition, attr, True)
    elif game_ids is None:
        setattr(competition, attr, False)


async def normalize_shot_events(
    db: AsyncSession,
    *,
    year: int,
    league_id: str,
    raw_root: Path,
    limit_games: int | None = None,
    game_ids: set[str] | None = None,
) -> SummerLeagueShotEventReport:
    """Normalize shot events from shotchartdetail snapshots for one slice.

    Reads each game's shotchartdetail.json, parses shot rows, resolves players
    via the shared nba_stats_person_id resolver, and idempotently upserts into
    SummerLeagueShotEvent keyed on (nba_stats_game_id, nba_stats_game_event_id).

    Also updates SummerLeagueRawFile.parse_status for the shotchartdetail
    endpoint and sets SummerLeagueCompetition.shotchart_available only when
    at least one game has parsed shot rows.

    Args:
        db: Async database session (caller handles commit/rollback).
        year: Competition year.
        league_id: NBA.com LeagueID string.
        raw_root: Root directory for raw NBA Stats snapshots.
        limit_games: Optional cap on the first N discovered games (by manifest
            order) processed -- test-only, see :func:`_limited_game_ids`.
        game_ids: Optional explicit set of game IDs to process, e.g. one
            batch of a larger venue (see
            ``app.cli.summer_league_ingest_runner``). Distinct from
            ``limit_games``: this selects an arbitrary subset rather than a
            prefix of the discovery order. When both are given, only games
            satisfying both filters are processed. ``None`` (the default)
            processes every game, exactly like before this parameter existed.

    Returns:
        Structured report with upsert counts.
    """
    competition = await _get_competition(db, year=year, league_id=league_id)
    if competition.id is None:
        raise RuntimeError("Competition id was not populated")

    season_dir = raw_root / f"{year}/{league_id}"
    effective_game_ids = _combine_game_id_filters(
        _limited_game_ids(
            raw_root=raw_root,
            year=year,
            league_id=league_id,
            limit_games=limit_games,
        ),
        game_ids,
    )
    games_by_source_id = await _games_by_source_id(db, competition.id)
    teams_by_source_id = await _teams_by_source_id(db, competition.id)
    raw_files_by_game = await _shot_raw_files_by_game(
        db,
        raw_run_id=competition.raw_run_id,
    )

    # Box/season-log lines per game, used to crosswalk legacy shotchartdetail
    # person-ids onto canonical box person-ids (issue #467). Season log covers
    # pre-2017 years (empty per-game box); per-game box covers modern years.
    box_rows_by_game: dict[str, list[ParsedPlayerBoxRow]] = {}
    for box_row in (
        *parse_player_gamelog_box_rows(season_dir / "leaguegamelog_player.json"),
        *parse_player_box_rows(season_dir, game_ids=effective_game_ids),
    ):
        box_rows_by_game.setdefault(box_row.game_id, []).append(box_row)

    games_processed = 0
    games_with_shots = 0
    games_root = season_dir / "games"

    # Pass 1: parse + crosswalk-remap every game's shots up front (no DB
    # writes yet). ``pending`` holds every shot row this call will write,
    # paired with its already-resolved ``SummerLeagueGame`` row, so the
    # identity/write passes below can operate on the whole batch at once
    # instead of once per event (#627).
    pending: list[tuple[SummerLeagueGame, ParsedShotEvent]] = []
    for game_dir in _iter_game_dirs(games_root, effective_game_ids):
        nba_game_id = game_dir.name
        shot_path = game_dir / "shotchartdetail.json"
        if not shot_path.exists():
            continue

        games_processed += 1
        game = games_by_source_id.get(nba_game_id)
        if game is None or game.id is None:
            continue

        shot_rows = parse_shot_rows(shot_path)

        game_box_rows = box_rows_by_game.get(nba_game_id, [])
        crosswalk = build_shot_player_crosswalk(shot_rows, game_box_rows)
        if crosswalk:
            box_names = {
                row.nba_stats_person_id: row.raw_player_name for row in game_box_rows
            }
            shot_rows = [
                _remap_shot_row(shot, crosswalk, box_names) for shot in shot_rows
            ]

        raw_file = raw_files_by_game.get(nba_game_id)
        if raw_file is not None:
            raw_file.parse_status = SummerLeagueRawFileStatus.PARSED
            raw_file.updated_at = _utc_now_naive()

        if not shot_rows:
            continue

        games_with_shots += 1
        pending.extend((game, shot_row) for shot_row in shot_rows)

    # Pass 2: preload/bulk-upsert every distinct source-player identity this
    # batch touches in one chunked statement (mirrors ``_upsert_source_player``
    # semantics exactly -- last-row-wins per person id, same as the row loop
    # it replaces, since a dict keyed by person id keeps only the final
    # write). Note this runs even for a shot whose team never resolves below,
    # matching the original loop's unconditional ``_upsert_source_player``
    # call before its team/source-player guard.
    identities: dict[str, ParsedPlayerGamelogRow] = {}
    for _game, shot_row in pending:
        identities[shot_row.nba_stats_person_id] = ParsedPlayerGamelogRow(
            nba_stats_person_id=shot_row.nba_stats_person_id,
            raw_player_name=shot_row.raw_player_name,
            nba_stats_team_id=shot_row.nba_stats_team_id,
        )
    source_refs = await _bulk_upsert_source_players(db, identities, year=year)

    # Pass 3: build one row per shot (last-row-wins per (game, event) key,
    # matching the idempotent upsert it replaces) and write the whole batch
    # via chunked INSERT ... ON CONFLICT.
    total_upserted = 0
    now = _utc_now_naive()
    shot_event_rows: dict[tuple[str, int], dict[str, Any]] = {}
    for game, shot_row in pending:
        source_ref = source_refs.get(shot_row.nba_stats_person_id)
        team = teams_by_source_id.get(shot_row.nba_stats_team_id)
        if team is None or team.id is None or source_ref is None:
            continue

        total_upserted += 1
        key = (shot_row.nba_stats_game_id, shot_row.nba_stats_game_event_id)
        shot_event_rows[key] = {
            "game_id": game.id,
            "competition_id": competition.id,
            "team_entry_id": team.id,
            "source_player_id": source_ref.id,
            "player_id": source_ref.canonical_player_id,
            "nba_stats_person_id": shot_row.nba_stats_person_id,
            "nba_stats_game_id": shot_row.nba_stats_game_id,
            "nba_stats_game_event_id": shot_row.nba_stats_game_event_id,
            "period": shot_row.period,
            "minutes_remaining": shot_row.minutes_remaining,
            "seconds_remaining": shot_row.seconds_remaining,
            "loc_x": shot_row.loc_x,
            "loc_y": shot_row.loc_y,
            "shot_distance": shot_row.shot_distance,
            "shot_type": shot_row.shot_type,
            "shot_zone_basic": shot_row.shot_zone_basic,
            "shot_zone_area": shot_row.shot_zone_area,
            "shot_zone_range": shot_row.shot_zone_range,
            "action_type": shot_row.action_type,
            "made": shot_row.made,
            "created_at": now,
            "updated_at": now,
        }

    await _bulk_upsert_shot_events(db, list(shot_event_rows.values()))
    # Flush first so any still-pending ORM changes (e.g. the raw_file
    # parse_status/updated_at writes above) are persisted before expiring --
    # expire() discards *unflushed* in-memory state, it does not flush it.
    # Then expire only SummerLeagueSourcePlayer/SummerLeagueShotEvent
    # instances (see :func:`_expire_written_instances`) -- see that
    # function's docstring for why a blanket ``db.expire_all()`` is unsafe
    # here.
    await db.flush()
    _expire_written_instances(db, (SummerLeagueSourcePlayer, SummerLeagueShotEvent))

    _raise_availability_flag(
        competition,
        "shotchart_available",
        has_events=games_with_shots > 0,
        game_ids=game_ids,
    )
    competition.updated_at = _utc_now_naive()
    await db.flush()

    return SummerLeagueShotEventReport(
        year=year,
        league_id=league_id,
        competition_id=competition.id,
        shot_events_upserted=total_upserted,
        games_processed=games_processed,
        games_with_shots=games_with_shots,
    )


async def normalize_pbp_events(
    db: AsyncSession,
    *,
    year: int,
    league_id: str,
    raw_root: Path,
    limit_games: int | None = None,
    game_ids: set[str] | None = None,
) -> SummerLeaguePBPEventReport:
    """Normalize PBP events from playbyplayv2 snapshots for one slice.

    Reads each game's playbyplayv2.json, parses play-by-play rows, resolves
    actor person IDs via the shared nba_stats_person_id resolver, and
    idempotently upserts into SummerLeaguePlayByPlayEvent keyed on
    (nba_stats_game_id, event_num).

    Also updates SummerLeagueRawFile.parse_status for the playbyplayv2
    endpoint and sets SummerLeagueCompetition.pbp_available only when at
    least one game has parsed PBP rows.  A game with no PBP file yields zero
    events and pbp_available stays False.

    Args:
        db: Async database session (caller handles commit/rollback).
        year: Competition year.
        league_id: NBA.com LeagueID string.
        raw_root: Root directory for raw NBA Stats snapshots.
        limit_games: Optional cap on the first N discovered games (by manifest
            order) processed -- test-only, see :func:`_limited_game_ids`.
        game_ids: Optional explicit set of game IDs to process, e.g. one
            batch of a larger venue (see
            ``app.cli.summer_league_ingest_runner``). Distinct from
            ``limit_games``: this selects an arbitrary subset rather than a
            prefix of the discovery order. When both are given, only games
            satisfying both filters are processed. ``None`` (the default)
            processes every game, exactly like before this parameter existed.

    Returns:
        Structured report with upsert counts.
    """
    competition = await _get_competition(db, year=year, league_id=league_id)
    if competition.id is None:
        raise RuntimeError("Competition id was not populated")

    season_dir = raw_root / f"{year}/{league_id}"
    effective_game_ids = _combine_game_id_filters(
        _limited_game_ids(
            raw_root=raw_root,
            year=year,
            league_id=league_id,
            limit_games=limit_games,
        ),
        game_ids,
    )
    games_by_source_id = await _games_by_source_id(db, competition.id)
    raw_files_by_game = await _pbp_raw_files_by_game(
        db,
        raw_run_id=competition.raw_run_id,
    )

    games_processed = 0
    games_with_pbp = 0
    games_root = season_dir / "games"

    # Pass 1: parse every game's PBP rows up front (no DB writes yet).
    pending: list[tuple[SummerLeagueGame, ParsedPBPEvent]] = []
    for game_dir in _iter_game_dirs(games_root, effective_game_ids):
        nba_game_id = game_dir.name
        pbp_path = game_dir / "playbyplayv2.json"
        if not pbp_path.exists():
            continue

        games_processed += 1
        game = games_by_source_id.get(nba_game_id)
        if game is None or game.id is None:
            continue

        pbp_rows = parse_pbp_rows(pbp_path)

        raw_file = raw_files_by_game.get(nba_game_id)
        if raw_file is not None:
            raw_file.parse_status = SummerLeagueRawFileStatus.PARSED
            raw_file.updated_at = _utc_now_naive()

        if not pbp_rows:
            continue

        games_with_pbp += 1
        pending.extend((game, pbp_row) for pbp_row in pbp_rows)

    # Pass 2: preload every distinct actor id referenced anywhere in the
    # batch's person1/person2/person3 columns in one chunked SELECT, instead
    # of up to three SELECTs per row (mirrors ``_resolve_actor_id``'s
    # lookup-only semantics -- never creates a source player).
    actor_ids: set[str] = set()
    for _game, pbp_row in pending:
        for person_id in (
            pbp_row.person1_nba_id,
            pbp_row.person2_nba_id,
            pbp_row.person3_nba_id,
        ):
            if person_id:
                actor_ids.add(person_id)
    actor_map = await _preload_actor_ids(db, actor_ids)

    # Pass 3: build one row per PBP event (last-row-wins per (game, event_num)
    # key, matching the idempotent upsert it replaces) and write the whole
    # batch via chunked INSERT ... ON CONFLICT.
    total_upserted = len(pending)
    now = _utc_now_naive()
    pbp_event_rows: dict[tuple[str, int], dict[str, Any]] = {}
    for game, pbp_row in pending:
        pbp_event_rows[(pbp_row.nba_stats_game_id, pbp_row.event_num)] = {
            "game_id": game.id,
            "competition_id": competition.id,
            "nba_stats_game_id": pbp_row.nba_stats_game_id,
            "event_num": pbp_row.event_num,
            "period": pbp_row.period,
            "clock": pbp_row.clock,
            "event_msg_type": pbp_row.event_msg_type,
            "home_score": pbp_row.home_score,
            "away_score": pbp_row.away_score,
            "score_margin": pbp_row.score_margin,
            "person1_nba_id": pbp_row.person1_nba_id,
            "person1_id": (
                actor_map.get(pbp_row.person1_nba_id)
                if pbp_row.person1_nba_id
                else None
            ),
            "person2_nba_id": pbp_row.person2_nba_id,
            "person2_id": (
                actor_map.get(pbp_row.person2_nba_id)
                if pbp_row.person2_nba_id
                else None
            ),
            "person3_nba_id": pbp_row.person3_nba_id,
            "person3_id": (
                actor_map.get(pbp_row.person3_nba_id)
                if pbp_row.person3_nba_id
                else None
            ),
            "description": pbp_row.description,
            "created_at": now,
            "updated_at": now,
        }

    await _bulk_upsert_pbp_events(db, list(pbp_event_rows.values()))
    # Flush pending ORM changes before expiring, then expire only
    # SummerLeagueSourcePlayer/SummerLeaguePlayByPlayEvent instances -- see
    # :func:`_expire_written_instances` and the matching comment in
    # :func:`normalize_shot_events`.
    await db.flush()
    _expire_written_instances(
        db, (SummerLeagueSourcePlayer, SummerLeaguePlayByPlayEvent)
    )

    _raise_availability_flag(
        competition,
        "pbp_available",
        has_events=games_with_pbp > 0,
        game_ids=game_ids,
    )
    competition.updated_at = _utc_now_naive()
    await db.flush()

    return SummerLeaguePBPEventReport(
        year=year,
        league_id=league_id,
        competition_id=competition.id,
        pbp_events_upserted=total_upserted,
        games_processed=games_processed,
        games_with_pbp=games_with_pbp,
    )


def parse_team_gamelog(path: Path) -> list[ParsedTeamGamelogRow]:
    """Parse source team gamelog rows."""
    payload = _read_payload(path)
    result_sets = extract_result_sets(payload)
    if not result_sets:
        return []
    result_set = result_sets[0]
    return [_parse_team_gamelog_row(result_set, row) for row in result_set.rows]


def parse_team_box_rows(
    season_dir: Path,
    *,
    game_ids: set[str] | None = None,
) -> list[ParsedTeamBoxRow]:
    """Parse team box-score rows from all game directories in one season."""
    rows: list[ParsedTeamBoxRow] = []
    games_root = season_dir / "games"
    if not games_root.exists():
        return rows
    for game_dir in _iter_game_dirs(games_root, game_ids):
        traditional = _team_stats_by_team_id(
            game_dir / "boxscoretraditionalv2.json",
            traditional=True,
        )
        advanced = _team_stats_by_team_id(
            game_dir / "boxscoreadvancedv2.json",
            traditional=False,
        )
        for team_id, traditional_row in traditional.items():
            advanced_row = advanced.get(team_id)
            rows.append(_merge_team_box_rows(traditional_row, advanced_row))
    return rows


def parse_player_gamelog(path: Path) -> list[ParsedPlayerGamelogRow]:
    """Parse source player identity rows from the season player gamelog."""
    payload = _read_payload(path)
    result_sets = extract_result_sets(payload)
    if not result_sets:
        return []
    result_set = result_sets[0]
    rows: list[ParsedPlayerGamelogRow] = []
    for row in result_set.rows:
        row_map = _row_map(result_set.headers, row)
        parsed = _parse_player_gamelog_row(row_map)
        if parsed is not None:
            rows.append(parsed)
    return rows


def parse_player_gamelog_box_rows(path: Path) -> list[ParsedPlayerBoxRow]:
    """Parse full player box lines from the season LeagueGameLog snapshot.

    Older Summer League years (pre-2017) have no per-game ``boxscoretraditionalv2``
    data at stats.nba.com, but the season-level LeagueGameLog carries a complete
    traditional line per player-game (MIN/PTS/FG/reb/etc.). This lets the
    normalizer build player game logs for those years. Advanced and scoring
    fields only come from the per-game endpoints, so they stay ``None`` here.

    Args:
        path: Path to ``leaguegamelog_player.json``.

    Returns:
        Parsed traditional box rows; empty when the file is missing or has no rows.
    """
    payload = _read_payload(path)
    result_sets = extract_result_sets(payload)
    if not result_sets:
        return []
    result_set = result_sets[0]
    rows: list[ParsedPlayerBoxRow] = []
    for row in result_set.rows:
        row_map = _row_map(result_set.headers, row)
        game_id = _str_or_none(row_map.get("GAME_ID"))
        person_id = _str_or_none(row_map.get("PLAYER_ID"))
        team_id = _str_or_none(row_map.get("TEAM_ID"))
        if not game_id or not person_id or not team_id:
            continue
        rows.append(
            ParsedPlayerBoxRow(
                game_id=game_id,
                nba_stats_person_id=person_id,
                raw_player_name=_str_or_none(row_map.get("PLAYER_NAME")) or "",
                nba_stats_team_id=team_id,
                minutes_seconds=parse_minutes_to_seconds(row_map.get("MIN")),
                pts=_int_or_none(row_map.get("PTS")),
                fgm=_int_or_none(row_map.get("FGM")),
                fga=_int_or_none(row_map.get("FGA")),
                fg_pct=_float_or_none(row_map.get("FG_PCT")),
                fg3m=_int_or_none(row_map.get("FG3M")),
                fg3a=_int_or_none(row_map.get("FG3A")),
                fg3_pct=_float_or_none(row_map.get("FG3_PCT")),
                ftm=_int_or_none(row_map.get("FTM")),
                fta=_int_or_none(row_map.get("FTA")),
                ft_pct=_float_or_none(row_map.get("FT_PCT")),
                oreb=_int_or_none(row_map.get("OREB")),
                dreb=_int_or_none(row_map.get("DREB")),
                reb=_int_or_none(row_map.get("REB")),
                ast=_int_or_none(row_map.get("AST")),
                stl=_int_or_none(row_map.get("STL")),
                blk=_int_or_none(row_map.get("BLK")),
                tov=_int_or_none(row_map.get("TOV")),
                pf=_int_or_none(row_map.get("PF")),
                plus_minus=_int_or_none(row_map.get("PLUS_MINUS")),
            )
        )
    return rows


def parse_player_box_rows(
    season_dir: Path,
    *,
    game_ids: set[str] | None = None,
) -> list[ParsedPlayerBoxRow]:
    """Parse player box-score rows from all game directories in one season."""
    rows: list[ParsedPlayerBoxRow] = []
    games_root = season_dir / "games"
    if not games_root.exists():
        return rows
    for game_dir in _iter_game_dirs(games_root, game_ids):
        traditional = _player_stats_by_key(
            game_dir / "boxscoretraditionalv2.json",
            stat_set="traditional",
        )
        advanced = _player_stats_by_key(
            game_dir / "boxscoreadvancedv2.json",
            stat_set="advanced",
        )
        scoring = _player_stats_by_key(
            game_dir / "boxscorescoringv2.json",
            stat_set="scoring",
        )
        for key, traditional_row in traditional.items():
            rows.append(
                _merge_player_box_rows(
                    traditional_row,
                    advanced.get(key),
                    scoring.get(key),
                )
            )
    return rows


def parse_shot_rows(path: Path) -> list[ParsedShotEvent]:
    """Parse shot attempt rows from a shotchartdetail JSON snapshot.

    Args:
        path: Path to the shotchartdetail.json file.

    Returns:
        Parsed shot event rows; empty list when the file is missing or has no rows.
    """
    payload = _read_payload(path)
    result_sets = extract_result_sets(payload)
    shot_set = next(
        (rs for rs in result_sets if rs.name == "Shot_Chart_Detail"),
        None,
    )
    if shot_set is None:
        return []
    rows: list[ParsedShotEvent] = []
    for row in shot_set.rows:
        row_map = _row_map(shot_set.headers, row)
        parsed = _parse_shot_row(row_map)
        if parsed is not None:
            rows.append(parsed)
    return rows


def build_shot_player_crosswalk(
    shot_rows: list[ParsedShotEvent],
    box_rows: list[ParsedPlayerBoxRow],
) -> dict[str, str]:
    """Map legacy shotchartdetail person-ids to canonical box person-ids for a game.

    Pre-2017 ``shotchartdetail`` returns a legacy 5-digit ``PLAYER_ID`` namespace
    for undrafted players (with ``PLAYER_NAME`` null) that does not match the
    canonical NBA person-ids carried by the box/season logs. Because a shot's
    person-id drives which ``SummerLeagueSourcePlayer`` it upserts against, those
    shots never inherit the box player's canonical id and per-player charts render
    empty (issue #467).

    We fingerprint every player by their ``(FGA, FGM, 3PA, 3PM)`` line — derivable
    from both the shot rows and the box rows — and map a legacy shot id to a box
    id only when that fingerprint is a *bijectively unique* match within the team
    (exactly one legacy shot player and exactly one box player share it). Ambiguous
    or unmatched ids are omitted so the shot stays unresolved rather than guessing.

    Args:
        shot_rows: Parsed shot events for a single game.
        box_rows: Parsed box/season-log lines for the same game (canonical ids).

    Returns:
        Mapping of legacy shot ``nba_stats_person_id`` to canonical box
        ``nba_stats_person_id``. Empty when nothing matches uniquely.
    """
    box_person_ids = {row.nba_stats_person_id for row in box_rows}

    box_by_sig: dict[tuple[str, tuple[int, int, int, int]], list[str]] = {}
    for row in box_rows:
        box_sig = (row.fga or 0, row.fgm or 0, row.fg3a or 0, row.fg3m or 0)
        box_by_sig.setdefault((row.nba_stats_team_id, box_sig), []).append(
            row.nba_stats_person_id
        )

    shot_agg: dict[tuple[str, str], list[int]] = {}
    for shot in shot_rows:
        key = (shot.nba_stats_team_id, shot.nba_stats_person_id)
        agg = shot_agg.setdefault(key, [0, 0, 0, 0])
        is_three = bool(shot.shot_type and "3PT" in shot.shot_type)
        agg[0] += 1
        if shot.made:
            agg[1] += 1
        if is_three:
            agg[2] += 1
            if shot.made:
                agg[3] += 1

    def _sig_key(team_id: str, agg: list[int]) -> tuple[str, tuple[int, int, int, int]]:
        return team_id, (agg[0], agg[1], agg[2], agg[3])

    # Count shot-side signature occurrences so a collision (two players with the
    # same line on one team) is treated as ambiguous and skipped.
    shot_sig_counts: Counter[tuple[str, tuple[int, int, int, int]]] = Counter(
        _sig_key(team_id, agg)
        for (team_id, person_id), agg in shot_agg.items()
        if person_id not in box_person_ids
    )

    crosswalk: dict[str, str] = {}
    for (team_id, person_id), agg in shot_agg.items():
        if person_id in box_person_ids:
            continue  # already a canonical id — no remap needed
        sig = _sig_key(team_id, agg)
        if shot_sig_counts[sig] != 1:
            continue  # ambiguous on the shot side
        candidates = box_by_sig.get(sig, [])
        if len(candidates) == 1:
            crosswalk[person_id] = candidates[0]
    return crosswalk


def _remap_shot_row(
    shot: ParsedShotEvent,
    crosswalk: dict[str, str],
    box_names: dict[str, str],
) -> ParsedShotEvent:
    """Rewrite a shot's legacy person-id to its canonical id via the crosswalk.

    Also adopts the canonical box player's name (the legacy shot row has none),
    which helps the shared resolver match the reused source player.
    """
    canonical_id = crosswalk.get(shot.nba_stats_person_id)
    if canonical_id is None:
        return shot
    return replace(
        shot,
        nba_stats_person_id=canonical_id,
        raw_player_name=box_names.get(canonical_id) or shot.raw_player_name,
    )


def parse_pbp_rows(path: Path) -> list[ParsedPBPEvent]:
    """Parse play-by-play event rows from a playbyplayv2 JSON snapshot.

    Args:
        path: Path to the playbyplayv2.json file.

    Returns:
        Parsed PBP event rows; empty list when the file is missing or has no rows.
    """
    payload = _read_payload(path)
    result_sets = extract_result_sets(payload)
    pbp_set = next(
        (rs for rs in result_sets if rs.name == "PlayByPlay"),
        None,
    )
    if pbp_set is None:
        return []
    rows: list[ParsedPBPEvent] = []
    for row in pbp_set.rows:
        row_map = _row_map(pbp_set.headers, row)
        parsed = _parse_pbp_row(row_map)
        if parsed is not None:
            rows.append(parsed)
    return rows


def parse_minutes_to_int(value: object) -> int | None:
    """Parse NBA.com minute values into an integer minute count."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value)
    if ":" in text:
        minutes, _seconds = text.split(":", 1)
        return _int_or_none(minutes)
    return _int_or_none(text)


def parse_minutes_to_seconds(value: object) -> int | None:
    """Parse NBA.com player minute values into total seconds."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(float(value) * 60)
    text = str(value).strip()
    if not text:
        return None
    if ":" in text:
        minutes, seconds = text.split(":", 1)
        parsed_minutes = _int_or_none(minutes)
        parsed_seconds = _int_or_none(seconds)
        if parsed_minutes is None or parsed_seconds is None:
            return None
        return parsed_minutes * 60 + parsed_seconds
    decimal_minutes = _float_or_none(text)
    return None if decimal_minutes is None else int(decimal_minutes * 60)


def team_slug(raw_team_name: str, abbreviation: str | None) -> str:
    """Return a stable slug for source team entries."""
    base = raw_team_name.strip().lower() or (abbreviation or "team").lower()
    slug = "".join(ch if ch.isalnum() else "-" for ch in base)
    return "-".join(part for part in slug.split("-") if part) or "team"


def _iter_game_dirs(games_root: Path, game_ids: set[str] | None) -> list[Path]:
    if not games_root.exists():
        return []
    game_dirs = sorted(path for path in games_root.iterdir() if path.is_dir())
    if game_ids is None:
        return game_dirs
    return [path for path in game_dirs if path.name in game_ids]


def _limited_game_ids(
    *,
    raw_root: Path,
    year: int,
    league_id: str,
    limit_games: int | None,
) -> set[str] | None:
    if limit_games is None:
        return None
    if limit_games < 0:
        raise ValueError("limit_games must be non-negative")
    manifest_path = raw_root / f"{year}/{league_id}/manifest.json"
    payload = _read_payload(manifest_path)
    game_ids = [str(value) for value in payload.get("game_ids") or []]
    return set(game_ids[:limit_games])


def _combine_game_id_filters(
    limited_game_ids: set[str] | None,
    game_ids: set[str] | None,
) -> set[str] | None:
    """Merge the test-only ``limit_games`` prefix filter with an explicit batch filter.

    Both :func:`normalize_shot_events` and :func:`normalize_pbp_events`
    accept two independent, optional game-id restrictions: ``limit_games``
    (a first-N-by-manifest-order prefix, used only by tests) and ``game_ids``
    (an arbitrary caller-selected subset, used by
    ``app.cli.summer_league_ingest_runner`` to process one batch of a
    venue). Neither set alone means "process everything"; only both being
    ``None`` does.

    Args:
        limited_game_ids: The resolved ``limit_games`` prefix, or ``None``.
        game_ids: The caller's explicit batch filter, or ``None``.

    Returns:
        ``None`` when neither filter is set (process every game); otherwise
        the intersection when both are set, or whichever single filter is
        set.
    """
    if limited_game_ids is None:
        return game_ids
    if game_ids is None:
        return limited_game_ids
    return limited_game_ids & game_ids


def _filter_team_gamelog_rows(
    rows: list[ParsedTeamGamelogRow],
    game_ids: set[str] | None,
) -> list[ParsedTeamGamelogRow]:
    if game_ids is None:
        return rows
    return [row for row in rows if row.game_id in game_ids]


async def _get_raw_run(
    db: AsyncSession,
    *,
    year: int,
    league_id: str,
) -> SummerLeagueRawRun:
    result = await db.execute(
        select(SummerLeagueRawRun).where(
            SummerLeagueRawRun.year == year,  # type: ignore[arg-type]
            SummerLeagueRawRun.league_id == league_id,  # type: ignore[arg-type]
        )
    )
    raw_run = result.scalar_one_or_none()
    if raw_run is None or raw_run.id is None:
        raise ValueError(f"No audited Summer League raw run for {year}/{league_id}")
    return raw_run


async def _get_competition(
    db: AsyncSession,
    *,
    year: int,
    league_id: str,
) -> SummerLeagueCompetition:
    result = await db.execute(
        select(SummerLeagueCompetition).where(
            SummerLeagueCompetition.year == year,  # type: ignore[arg-type]
            SummerLeagueCompetition.league_id == league_id,  # type: ignore[arg-type]
        )
    )
    competition = result.scalar_one_or_none()
    if competition is None or competition.id is None:
        raise ValueError(
            f"No normalized Summer League competition for {year}/{league_id}"
        )
    return competition


async def _get_raw_files(
    db: AsyncSession, *, raw_run_id: int
) -> list[SummerLeagueRawFile]:
    result = await db.execute(
        select(SummerLeagueRawFile).where(
            SummerLeagueRawFile.raw_run_id == raw_run_id  # type: ignore[arg-type]
        )
    )
    return list(result.scalars().all())


def _competition_quality(
    raw_run: SummerLeagueRawRun,
    raw_files: list[SummerLeagueRawFile],
) -> SummerLeagueDataQuality:
    statuses = {file.parse_status for file in raw_files}
    parsed_endpoints = {
        file.endpoint
        for file in raw_files
        if file.parse_status == SummerLeagueRawFileStatus.PARSED
    }
    has_box = "boxscoretraditionalv2" in parsed_endpoints
    has_pbp = "playbyplayv2" in parsed_endpoints
    has_shots = "shotchartdetail" in parsed_endpoints
    if raw_run.status == "COMPLETE" and has_box and has_pbp and has_shots:
        return SummerLeagueDataQuality.FULL
    if has_box and not (has_pbp or has_shots):
        return SummerLeagueDataQuality.BOX_ONLY
    if statuses and statuses != {SummerLeagueRawFileStatus.MISSING}:
        return SummerLeagueDataQuality.PARTIAL
    return SummerLeagueDataQuality.RAW_ONLY


async def _upsert_competition(
    db: AsyncSession,
    raw_run: SummerLeagueRawRun,
    quality: SummerLeagueDataQuality,
) -> SummerLeagueCompetition:
    result = await db.execute(
        select(SummerLeagueCompetition).where(
            SummerLeagueCompetition.year == raw_run.year,  # type: ignore[arg-type]
            SummerLeagueCompetition.league_id == raw_run.league_id,  # type: ignore[arg-type]
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        row = SummerLeagueCompetition(
            year=raw_run.year,
            league_id=raw_run.league_id,
            venue_slug=raw_run.venue_slug,
            display_name=_display_name(raw_run.year, raw_run.venue_slug),
        )
        db.add(row)
    row.venue_slug = raw_run.venue_slug
    row.display_name = _display_name(raw_run.year, raw_run.venue_slug)
    row.data_quality = quality
    row.pbp_available = quality == SummerLeagueDataQuality.FULL
    row.shotchart_available = quality == SummerLeagueDataQuality.FULL
    row.raw_run_id = raw_run.id
    row.updated_at = _utc_now_naive()
    return row


async def _upsert_team_entry(
    db: AsyncSession,
    competition_id: int,
    row_data: ParsedTeamGamelogRow,
) -> SummerLeagueTeamEntry:
    result = await db.execute(
        select(SummerLeagueTeamEntry).where(
            SummerLeagueTeamEntry.competition_id == competition_id,  # type: ignore[arg-type]
            SummerLeagueTeamEntry.nba_stats_team_id == row_data.nba_stats_team_id,  # type: ignore[arg-type]
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        row = SummerLeagueTeamEntry(
            competition_id=competition_id,
            nba_stats_team_id=row_data.nba_stats_team_id,
            raw_team_name=row_data.raw_team_name,
            team_slug=team_slug(row_data.raw_team_name, row_data.raw_team_abbreviation),
        )
        db.add(row)
    row.raw_team_name = row_data.raw_team_name
    row.raw_team_abbreviation = row_data.raw_team_abbreviation
    row.team_slug = team_slug(row_data.raw_team_name, row_data.raw_team_abbreviation)
    row.updated_at = _utc_now_naive()
    return row


def resolve_game_status(
    *,
    current_status: SummerLeagueGameStatus,
    raw_run_complete: bool,
) -> SummerLeagueGameStatus:
    """Pure Job B status resolution for one ``summer_league_games`` row (#530).

    Normalization alone must never promote a game from Scheduled/In-Progress
    to Final just because *some* raw box data for it happened to parse this
    pass -- a live tick's targeted raw refresh
    (``app.services.summer_league.live_ingestion``) can legitimately force a
    fresh boxscore snapshot for a game that is nowhere near over, and Job B's
    tick order (scoreboard -> targeted raw refresh -> normalization) means
    ``current_status`` already carries whatever the scoreboard step (the
    provider-authoritative source for live status, #529) most recently wrote
    onto this row. Only two things ever establish Final here:

    1. Scoreboard's own provider truth, already reflected in
       ``current_status`` by the time this runs.
    2. A fully audited, ``COMPLETE`` raw run (``raw_run_complete``) for a
       game scoreboard has never tracked at all (``current_status`` is
       ``UNKNOWN`` -- a historic year normalized straight from a one-shot
       full backfill with no live scoreboard step ever run against it). The
       whole audited slice being genuinely complete -- every discovered
       game's raw files successfully captured -- is real completion evidence
       there, matching this normalizer's original behavior for full historic
       ingests. A live event's raw run stays ``PARTIAL`` for as long as any
       game hasn't finished, so this path never fires mid-event.

    Once Final, monotonic: no later call -- partial, stale, or otherwise --
    can regress it back to Scheduled/In-Progress/Unknown. POSTPONED/CANCELED
    (fix #4) are likewise terminal and pass straight through here unchanged:
    a postponed game will never tip, so an audited-``COMPLETE`` raw run for
    its year/league (evidence the *other* games in that slice genuinely
    finished) must never be read as evidence that *this* game finished too
    and get promoted to FINAL -- nor may it ever regress back to
    Scheduled/In-Progress/Unknown.

    Args:
        current_status: The game's persisted status before this call (the
            existing row's ``status``, or ``UNKNOWN`` -- the schema default
            -- for a brand-new row).
        raw_run_complete: Whether the audited ``SummerLeagueRawRun`` driving
            this normalize call is ``COMPLETE`` (every expected raw file for
            the whole year/league successfully captured).

    Returns:
        The status to persist.
    """
    if current_status in (
        SummerLeagueGameStatus.FINAL,
        SummerLeagueGameStatus.POSTPONED,
        SummerLeagueGameStatus.CANCELED,
    ):
        return current_status
    if current_status in (
        SummerLeagueGameStatus.SCHEDULED,
        SummerLeagueGameStatus.IN_PROGRESS,
    ):
        return current_status
    if raw_run_complete:
        return SummerLeagueGameStatus.FINAL
    return SummerLeagueGameStatus.UNKNOWN


async def _upsert_game(
    db: AsyncSession,
    competition_id: int,
    game_id: str,
    rows: list[ParsedTeamGamelogRow],
    teams_by_source_id: dict[str, SummerLeagueTeamEntry],
    quality: SummerLeagueDataQuality,
    *,
    raw_run_complete: bool,
) -> SummerLeagueGame:
    result = await db.execute(
        select(SummerLeagueGame).where(
            SummerLeagueGame.nba_stats_game_id == game_id  # type: ignore[arg-type]
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        row = SummerLeagueGame(
            competition_id=competition_id,
            nba_stats_game_id=game_id,
        )
        db.add(row)
    home_row = _home_row(rows)
    away_row = next((item for item in rows if item is not home_row), None)
    home_team = teams_by_source_id.get(home_row.nba_stats_team_id) if home_row else None
    away_team = teams_by_source_id.get(away_row.nba_stats_team_id) if away_row else None
    row.competition_id = competition_id
    row.game_date = (
        home_row.game_date if home_row else (rows[0].game_date if rows else None)
    )
    row.home_team_entry_id = home_team.id if home_team else None
    row.away_team_entry_id = away_team.id if away_team else None
    row.home_score = home_row.pts if home_row else None
    row.away_score = away_row.pts if away_row else None
    row.status = resolve_game_status(
        current_status=row.status, raw_run_complete=raw_run_complete
    )
    row.source_quality = quality
    row.updated_at = _utc_now_naive()
    return row


async def _games_by_source_id(
    db: AsyncSession, competition_id: int
) -> dict[str, SummerLeagueGame]:
    result = await db.execute(
        select(SummerLeagueGame).where(
            SummerLeagueGame.competition_id == competition_id  # type: ignore[arg-type]
        )
    )
    return {game.nba_stats_game_id: game for game in result.scalars().all()}


async def _teams_by_source_id(
    db: AsyncSession, competition_id: int
) -> dict[str, SummerLeagueTeamEntry]:
    result = await db.execute(
        select(SummerLeagueTeamEntry).where(
            SummerLeagueTeamEntry.competition_id == competition_id  # type: ignore[arg-type]
        )
    )
    return {team.nba_stats_team_id: team for team in result.scalars().all()}


async def _participations_by_key(
    db: AsyncSession, competition_id: int
) -> dict[tuple[int, int], SummerLeagueParticipation]:
    """Map ``(team_entry_id, source_player_id)`` -> participation for a competition.

    Preloaded once per ingest so the box-row loop does an in-memory lookup instead
    of a SELECT per row (mirrors ``_games_by_source_id`` / ``_teams_by_source_id``).
    Ordered ascending by ``stint_no`` so the latest stint wins on collision (today
    there is exactly one participation per grain).
    """
    result = await db.execute(
        select(SummerLeagueParticipation)
        .where(SummerLeagueParticipation.competition_id == competition_id)  # type: ignore[arg-type]
        .order_by(SummerLeagueParticipation.stint_no.asc())  # type: ignore[attr-defined]
    )
    return {(p.team_entry_id, p.source_player_id): p for p in result.scalars().all()}


async def _upsert_team_game_log(
    db: AsyncSession,
    competition_id: int,
    game_id: int,
    team_entry_id: int,
    box_row: ParsedTeamBoxRow,
    *,
    source_endpoint: str = "boxscoretraditionalv2",
) -> SummerLeagueTeamGameLog:
    result = await db.execute(
        select(SummerLeagueTeamGameLog).where(
            SummerLeagueTeamGameLog.game_id == game_id,  # type: ignore[arg-type]
            SummerLeagueTeamGameLog.team_entry_id == team_entry_id,  # type: ignore[arg-type]
        )
    )
    row = result.scalar_one_or_none()
    # A targeted/live normalization can have only the season gamelog on disk
    # while an earlier full ingestion already persisted the official per-game
    # box.  The gamelog fallback has no team minutes, so replacing that richer
    # row would silently make an otherwise complete competition ineligible for
    # advanced metrics on a later tick.  Fallback data may create a missing
    # row, but it must never downgrade an official box-score row.
    if (
        row is not None
        and source_endpoint == "leaguegamelog_team"
        and row.source_endpoint != "leaguegamelog_team"
    ):
        return row
    if row is None:
        row = SummerLeagueTeamGameLog(
            competition_id=competition_id,
            game_id=game_id,
            team_entry_id=team_entry_id,
        )
        db.add(row)
    for field_name in ParsedTeamBoxRow.__dataclass_fields__:
        if field_name in {
            "game_id",
            "nba_stats_team_id",
            "raw_team_name",
            "raw_team_abbreviation",
        }:
            continue
        setattr(row, field_name, getattr(box_row, field_name))
    row.source_endpoint = source_endpoint
    row.updated_at = _utc_now_naive()
    return row


def _team_box_row_from_gamelog(row: ParsedTeamGamelogRow) -> ParsedTeamBoxRow:
    return ParsedTeamBoxRow(
        game_id=row.game_id,
        nba_stats_team_id=row.nba_stats_team_id,
        raw_team_name=row.raw_team_name,
        raw_team_abbreviation=row.raw_team_abbreviation,
        pts=row.pts,
    )


async def _upsert_source_player(
    db: AsyncSession,
    row_data: ParsedPlayerGamelogRow,
    *,
    year: int,
) -> SummerLeagueSourcePlayer:
    result = await db.execute(
        select(SummerLeagueSourcePlayer).where(
            SummerLeagueSourcePlayer.nba_stats_person_id == row_data.nba_stats_person_id  # type: ignore[arg-type]
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        row = SummerLeagueSourcePlayer(
            nba_stats_person_id=row_data.nba_stats_person_id,
            raw_player_name=row_data.raw_player_name,
            normalized_name=_normalized_name_key(row_data.raw_player_name),
            first_seen_year=year,
            last_seen_year=year,
            resolution_status=SummerLeagueResolutionStatus.UNRESOLVED,
        )
        db.add(row)
    row.raw_player_name = row_data.raw_player_name
    row.normalized_name = _normalized_name_key(row_data.raw_player_name)
    row.first_seen_year = (
        year if row.first_seen_year is None else min(row.first_seen_year, year)
    )
    row.last_seen_year = (
        year if row.last_seen_year is None else max(row.last_seen_year, year)
    )
    row.updated_at = _utc_now_naive()
    return row


async def _ensure_participation(
    db: AsyncSession,
    competition_id: int,
    team_entry_id: int,
    source_player: SummerLeagueSourcePlayer,
    *,
    cache: dict[tuple[int, int], SummerLeagueParticipation],
    recorded_at: datetime,
) -> SummerLeagueParticipation:
    """Return the stable participation bridge for a (competition, team, player).

    Player game logs reference this bridge rather than the raw (player, edition)
    pair, decoupling the stat layer from roster identity (journey-graph §7b). The
    bridge is looked up in ``cache`` (preloaded by ``_participations_by_key``), so
    the box-row loop issues no per-row SELECT.

    If roster ingest already announced this player the existing bridge is reused.
    Otherwise the player was discovered straight from a box score — a late add
    with no pre-event roster entry — and the bridge is born canonical: a
    ``CONFIRMED`` ``PlayerAffiliation`` assertion (the box score *is* the
    corroborating evidence) is written alongside the participation so the
    append-only affiliation stream is complete at birth, with no orphan
    ``affiliation_id = None`` rows left for a later reconcile pass to heal.

    Args:
        db: Async database session.
        competition_id: PK of the parent competition.
        team_entry_id: PK of the team entry the player appeared for.
        source_player: The resolved (or stub) source-player row; ``id`` must be
            populated.
        cache: Preloaded ``(team_entry_id, source_player_id)`` -> participation
            map; a newly-created bridge is added so later rows in the same run
            reuse it.
        recorded_at: Timestamp stamped on a newly-written affiliation assertion.

    Returns:
        The reused or newly-created ``SummerLeagueParticipation`` row, flushed so
        its ``id`` is available for stamping onto the game log.
    """
    source_player_id: int = source_player.id  # type: ignore[assignment]
    key = (team_entry_id, source_player_id)
    participation = cache.get(key)
    if participation is not None:
        # Keep the resolved canonical id in sync as resolution backfills it,
        # mirroring the game-log player_id behavior. The roster_status and the
        # announced/confirmed promotion are owned by the roster + reconcile layer
        # and left untouched here.
        if participation.player_id != source_player.canonical_player_id:
            participation.player_id = source_player.canonical_player_id
            participation.updated_at = _utc_now_naive()
        return participation

    affiliation = PlayerAffiliation(
        player_id=source_player.canonical_player_id,
        nba_team_id=None,
        affiliation_type=AffiliationType.SUMMER_LEAGUE_ROSTER,
        status=AffiliationStatus.CONFIRMED,
        recorded_at=recorded_at,
        source="nba_summer_league_box_score",
        source_ref=source_player.nba_stats_person_id,
    )
    db.add(affiliation)
    await db.flush()  # populate affiliation.id

    participation = SummerLeagueParticipation(
        competition_id=competition_id,
        team_entry_id=team_entry_id,
        source_player_id=source_player_id,
        player_id=source_player.canonical_player_id,
        affiliation_id=affiliation.id,
        stint_no=1,
        roster_status=AffiliationStatus.CONFIRMED,
    )
    db.add(participation)
    await db.flush()
    cache[key] = participation
    return participation


async def _upsert_player_game_log(
    db: AsyncSession,
    competition_id: int,
    game_id: int,
    team_entry_id: int,
    source_player: SummerLeagueSourcePlayer,
    box_row: ParsedPlayerBoxRow,
    *,
    participations: dict[tuple[int, int], SummerLeagueParticipation],
    recorded_at: datetime,
) -> SummerLeaguePlayerGameLog:
    result = await db.execute(
        select(SummerLeaguePlayerGameLog).where(
            SummerLeaguePlayerGameLog.game_id == game_id,  # type: ignore[arg-type]
            SummerLeaguePlayerGameLog.nba_stats_person_id
            == box_row.nba_stats_person_id,  # type: ignore[arg-type]
            SummerLeaguePlayerGameLog.team_entry_id == team_entry_id,  # type: ignore[arg-type]
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        if source_player.id is None:
            raise RuntimeError("Source player id was not populated")
        row = SummerLeaguePlayerGameLog(
            competition_id=competition_id,
            game_id=game_id,
            team_entry_id=team_entry_id,
            source_player_id=source_player.id,
            nba_stats_person_id=box_row.nba_stats_person_id,
            raw_player_name=box_row.raw_player_name,
        )
        db.add(row)
    if source_player.id is None:
        raise RuntimeError("Source player id was not populated")
    row.source_player_id = source_player.id
    # A live ingestion tick re-normalizes box scores before the separate
    # resolution pass.  Do not let a transiently absent source-player link
    # erase a previously resolved, completed game line; that makes the row
    # disappear from the season aggregate until resolution catches up.
    if source_player.canonical_player_id is not None or row.player_id is None:
        row.player_id = source_player.canonical_player_id
    participation = await _ensure_participation(
        db,
        competition_id,
        team_entry_id,
        source_player,
        cache=participations,
        recorded_at=recorded_at,
    )
    row.participation_id = participation.id
    row.raw_player_name = box_row.raw_player_name
    for field_name in ParsedPlayerBoxRow.__dataclass_fields__:
        if field_name in {
            "game_id",
            "nba_stats_person_id",
            "raw_player_name",
            "nba_stats_team_id",
        }:
            continue
        setattr(row, field_name, getattr(box_row, field_name))
    row.source_endpoint = "boxscoretraditionalv2"
    row.updated_at = _utc_now_naive()
    return row


def _parse_team_gamelog_row(
    result_set: NBAStatsResultSet,
    row: list[Any],
) -> ParsedTeamGamelogRow:
    row_map = _row_map(result_set.headers, row)
    return ParsedTeamGamelogRow(
        game_id=str(row_map.get("GAME_ID") or ""),
        game_date=_parse_date(row_map.get("GAME_DATE")),
        nba_stats_team_id=str(row_map.get("TEAM_ID") or ""),
        raw_team_name=str(row_map.get("TEAM_NAME") or ""),
        raw_team_abbreviation=_str_or_none(row_map.get("TEAM_ABBREVIATION")),
        matchup=_str_or_none(row_map.get("MATCHUP")),
        pts=_int_or_none(row_map.get("PTS")),
    )


def _parse_player_gamelog_row(
    row_map: dict[str, Any],
) -> ParsedPlayerGamelogRow | None:
    person_id = _source_player_id(row_map)
    name = _source_player_name(row_map)
    if not person_id or not name:
        return None
    return ParsedPlayerGamelogRow(
        nba_stats_person_id=person_id,
        raw_player_name=name,
        nba_stats_team_id=_str_or_none(row_map.get("TEAM_ID")),
    )


def _team_stats_by_team_id(
    path: Path, *, traditional: bool
) -> dict[str, ParsedTeamBoxRow]:
    payload = _read_payload(path)
    team_stats = next(
        (
            result_set
            for result_set in extract_result_sets(payload)
            if result_set.name == "TeamStats"
        ),
        None,
    )
    if team_stats is None:
        return {}
    rows: dict[str, ParsedTeamBoxRow] = {}
    for row in team_stats.rows:
        row_map = _row_map(team_stats.headers, row)
        team_id = str(row_map.get("TEAM_ID") or "")
        if not team_id:
            continue
        rows[team_id] = _parse_team_box_row(row_map, traditional=traditional)
    return rows


def _parse_team_box_row(
    row_map: dict[str, Any], *, traditional: bool
) -> ParsedTeamBoxRow:
    base = ParsedTeamBoxRow(
        game_id=str(row_map.get("GAME_ID") or ""),
        nba_stats_team_id=str(row_map.get("TEAM_ID") or ""),
        raw_team_name=str(row_map.get("TEAM_NAME") or ""),
        raw_team_abbreviation=_str_or_none(row_map.get("TEAM_ABBREVIATION")),
    )
    if traditional:
        return ParsedTeamBoxRow(
            game_id=base.game_id,
            nba_stats_team_id=base.nba_stats_team_id,
            raw_team_name=base.raw_team_name,
            raw_team_abbreviation=base.raw_team_abbreviation,
            minutes=parse_minutes_to_int(row_map.get("MIN")),
            pts=_int_or_none(row_map.get("PTS")),
            fgm=_int_or_none(row_map.get("FGM")),
            fga=_int_or_none(row_map.get("FGA")),
            fg_pct=_float_or_none(row_map.get("FG_PCT")),
            fg3m=_int_or_none(row_map.get("FG3M")),
            fg3a=_int_or_none(row_map.get("FG3A")),
            fg3_pct=_float_or_none(row_map.get("FG3_PCT")),
            ftm=_int_or_none(row_map.get("FTM")),
            fta=_int_or_none(row_map.get("FTA")),
            ft_pct=_float_or_none(row_map.get("FT_PCT")),
            oreb=_int_or_none(row_map.get("OREB")),
            dreb=_int_or_none(row_map.get("DREB")),
            reb=_int_or_none(row_map.get("REB")),
            ast=_int_or_none(row_map.get("AST")),
            stl=_int_or_none(row_map.get("STL")),
            blk=_int_or_none(row_map.get("BLK")),
            tov=_int_or_none(
                row_map.get("TO") if "TO" in row_map else row_map.get("TOV")
            ),
            pf=_int_or_none(row_map.get("PF")),
            plus_minus=_int_or_none(row_map.get("PLUS_MINUS")),
        )
    return ParsedTeamBoxRow(
        game_id=base.game_id,
        nba_stats_team_id=base.nba_stats_team_id,
        raw_team_name=base.raw_team_name,
        raw_team_abbreviation=base.raw_team_abbreviation,
        # Kept as the fallback source when the traditional MIN is corrupt.
        minutes=parse_minutes_to_int(row_map.get("MIN")),
        off_rating=_float_or_none(row_map.get("OFF_RATING")),
        def_rating=_float_or_none(row_map.get("DEF_RATING")),
        net_rating=_float_or_none(row_map.get("NET_RATING")),
        ast_pct=_float_or_none(row_map.get("AST_PCT")),
        reb_pct=_float_or_none(row_map.get("REB_PCT")),
        efg_pct=_float_or_none(row_map.get("EFG_PCT")),
        ts_pct=_float_or_none(row_map.get("TS_PCT")),
        pace=_float_or_none(row_map.get("PACE")),
    )


def _merge_team_box_rows(
    traditional: ParsedTeamBoxRow,
    advanced: ParsedTeamBoxRow | None,
) -> ParsedTeamBoxRow:
    values = traditional.__dict__.copy()
    if advanced is not None:
        for field_name in (
            "off_rating",
            "def_rating",
            "net_rating",
            "ast_pct",
            "reb_pct",
            "efg_pct",
            "ts_pct",
            "pace",
        ):
            values[field_name] = getattr(advanced, field_name)
    # Corrupt-source guard: an implausible traditional MIN falls back to the
    # advanced box's value, or to NULL when that is missing/equally corrupt.
    if values["minutes"] is not None and values["minutes"] > MAX_PLAUSIBLE_TEAM_MINUTES:
        adv_minutes = advanced.minutes if advanced is not None else None
        values["minutes"] = (
            adv_minutes
            if adv_minutes is not None and adv_minutes <= MAX_PLAUSIBLE_TEAM_MINUTES
            else None
        )
    return ParsedTeamBoxRow(**values)


PlayerBoxKey = tuple[str, str, str]


def _player_stats_by_key(
    path: Path, *, stat_set: str
) -> dict[PlayerBoxKey, ParsedPlayerBoxRow]:
    payload = _read_payload(path)
    result_set_names = (
        ("sqlPlayersScoring", "PlayerStats")
        if stat_set == "scoring"
        else ("PlayerStats",)
    )
    player_stats = next(
        (
            result_set
            for result_set in extract_result_sets(payload)
            if result_set.name in result_set_names
        ),
        None,
    )
    if player_stats is None:
        return {}
    rows: dict[PlayerBoxKey, ParsedPlayerBoxRow] = {}
    for row in player_stats.rows:
        row_map = _row_map(player_stats.headers, row)
        parsed = _parse_player_box_row(row_map, stat_set=stat_set)
        if parsed is None:
            continue
        rows[(parsed.game_id, parsed.nba_stats_person_id, parsed.nba_stats_team_id)] = (
            parsed
        )
    return rows


def _parse_player_box_row(
    row_map: dict[str, Any], *, stat_set: str
) -> ParsedPlayerBoxRow | None:
    person_id = _source_player_id(row_map)
    player_name = _source_player_name(row_map)
    game_id = str(row_map.get("GAME_ID") or "")
    team_id = str(row_map.get("TEAM_ID") or "")
    if not person_id or not player_name or not game_id or not team_id:
        return None

    base = ParsedPlayerBoxRow(
        game_id=game_id,
        nba_stats_person_id=person_id,
        raw_player_name=player_name,
        nba_stats_team_id=team_id,
    )
    if stat_set == "traditional":
        return ParsedPlayerBoxRow(
            game_id=base.game_id,
            nba_stats_person_id=base.nba_stats_person_id,
            raw_player_name=base.raw_player_name,
            nba_stats_team_id=base.nba_stats_team_id,
            starter_position=_str_or_none(row_map.get("START_POSITION")),
            comment=_str_or_none(row_map.get("COMMENT")),
            minutes_seconds=parse_minutes_to_seconds(row_map.get("MIN")),
            pts=_int_or_none(row_map.get("PTS")),
            fgm=_int_or_none(row_map.get("FGM")),
            fga=_int_or_none(row_map.get("FGA")),
            fg_pct=_float_or_none(row_map.get("FG_PCT")),
            fg3m=_int_or_none(row_map.get("FG3M")),
            fg3a=_int_or_none(row_map.get("FG3A")),
            fg3_pct=_float_or_none(row_map.get("FG3_PCT")),
            ftm=_int_or_none(row_map.get("FTM")),
            fta=_int_or_none(row_map.get("FTA")),
            ft_pct=_float_or_none(row_map.get("FT_PCT")),
            oreb=_int_or_none(row_map.get("OREB")),
            dreb=_int_or_none(row_map.get("DREB")),
            reb=_int_or_none(row_map.get("REB")),
            ast=_int_or_none(row_map.get("AST")),
            stl=_int_or_none(row_map.get("STL")),
            blk=_int_or_none(row_map.get("BLK")),
            tov=_int_or_none(
                row_map.get("TO") if "TO" in row_map else row_map.get("TOV")
            ),
            pf=_int_or_none(row_map.get("PF")),
            plus_minus=_int_or_none(row_map.get("PLUS_MINUS")),
        )
    if stat_set == "advanced":
        return ParsedPlayerBoxRow(
            game_id=base.game_id,
            nba_stats_person_id=base.nba_stats_person_id,
            raw_player_name=base.raw_player_name,
            nba_stats_team_id=base.nba_stats_team_id,
            # Kept as the fallback source when the traditional MIN is corrupt.
            minutes_seconds=parse_minutes_to_seconds(row_map.get("MIN")),
            off_rating=_float_or_none(row_map.get("OFF_RATING")),
            def_rating=_float_or_none(row_map.get("DEF_RATING")),
            net_rating=_float_or_none(row_map.get("NET_RATING")),
            ast_pct=_float_or_none(row_map.get("AST_PCT")),
            oreb_pct=_float_or_none(row_map.get("OREB_PCT")),
            dreb_pct=_float_or_none(row_map.get("DREB_PCT")),
            reb_pct=_float_or_none(row_map.get("REB_PCT")),
            tm_tov_pct=_float_or_none(row_map.get("TM_TOV_PCT")),
            efg_pct=_float_or_none(row_map.get("EFG_PCT")),
            ts_pct=_float_or_none(row_map.get("TS_PCT")),
            usg_pct=_float_or_none(row_map.get("USG_PCT")),
            pace=_float_or_none(row_map.get("PACE")),
            pie=_float_or_none(row_map.get("PIE")),
        )
    if stat_set == "scoring":
        return ParsedPlayerBoxRow(
            game_id=base.game_id,
            nba_stats_person_id=base.nba_stats_person_id,
            raw_player_name=base.raw_player_name,
            nba_stats_team_id=base.nba_stats_team_id,
            pct_fga_2pt=_float_or_none(row_map.get("PCT_FGA_2PT")),
            pct_fga_3pt=_float_or_none(row_map.get("PCT_FGA_3PT")),
            pct_pts_2pt=_float_or_none(row_map.get("PCT_PTS_2PT")),
            pct_pts_3pt=_float_or_none(row_map.get("PCT_PTS_3PT")),
            pct_pts_ft=_float_or_none(row_map.get("PCT_PTS_FT")),
        )
    raise ValueError(f"Unsupported player stat set: {stat_set}")


def _merge_player_box_rows(
    traditional: ParsedPlayerBoxRow,
    advanced: ParsedPlayerBoxRow | None,
    scoring: ParsedPlayerBoxRow | None,
) -> ParsedPlayerBoxRow:
    values = traditional.__dict__.copy()
    if advanced is not None:
        for field_name in (
            "off_rating",
            "def_rating",
            "net_rating",
            "ast_pct",
            "oreb_pct",
            "dreb_pct",
            "reb_pct",
            "tm_tov_pct",
            "efg_pct",
            "ts_pct",
            "usg_pct",
            "pace",
            "pie",
        ):
            values[field_name] = getattr(advanced, field_name)
    # Corrupt-source guard: an implausible traditional MIN falls back to the
    # advanced box's value, or to NULL when that is missing/equally corrupt.
    if (
        values["minutes_seconds"] is not None
        and values["minutes_seconds"] > MAX_PLAUSIBLE_PLAYER_SECONDS
    ):
        adv_seconds = advanced.minutes_seconds if advanced is not None else None
        values["minutes_seconds"] = (
            adv_seconds
            if adv_seconds is not None and adv_seconds <= MAX_PLAUSIBLE_PLAYER_SECONDS
            else None
        )
    if scoring is not None:
        for field_name in (
            "pct_fga_2pt",
            "pct_fga_3pt",
            "pct_pts_2pt",
            "pct_pts_3pt",
            "pct_pts_ft",
        ):
            values[field_name] = getattr(scoring, field_name)
    return ParsedPlayerBoxRow(**values)


def _group_game_rows(
    rows: list[ParsedTeamGamelogRow],
) -> dict[str, list[ParsedTeamGamelogRow]]:
    grouped: dict[str, list[ParsedTeamGamelogRow]] = {}
    for row in rows:
        grouped.setdefault(row.game_id, []).append(row)
    return grouped


def _home_row(rows: list[ParsedTeamGamelogRow]) -> ParsedTeamGamelogRow | None:
    return next(
        (row for row in rows if row.matchup and " vs. " in row.matchup),
        rows[0] if rows else None,
    )


def _parse_shot_row(row_map: dict[str, Any]) -> ParsedShotEvent | None:
    game_id = str(row_map.get("GAME_ID") or "")
    event_id_raw = row_map.get("GAME_EVENT_ID")
    person_id = str(row_map.get("PLAYER_ID") or "")
    team_id = str(row_map.get("TEAM_ID") or "")
    if not game_id or event_id_raw is None or not person_id or not team_id:
        return None
    event_id = _int_or_none(event_id_raw)
    if event_id is None:
        return None
    raw_name = str(row_map.get("PLAYER_NAME") or person_id)
    made_flag = _int_or_none(row_map.get("SHOT_MADE_FLAG"))
    return ParsedShotEvent(
        nba_stats_game_id=game_id,
        nba_stats_game_event_id=event_id,
        nba_stats_person_id=person_id,
        raw_player_name=raw_name,
        nba_stats_team_id=team_id,
        period=_int_or_none(row_map.get("PERIOD")),
        minutes_remaining=_int_or_none(row_map.get("MINUTES_REMAINING")),
        seconds_remaining=_int_or_none(row_map.get("SECONDS_REMAINING")),
        loc_x=_int_or_none(row_map.get("LOC_X")),
        loc_y=_int_or_none(row_map.get("LOC_Y")),
        shot_distance=_int_or_none(row_map.get("SHOT_DISTANCE")),
        shot_type=_str_or_none(row_map.get("SHOT_TYPE")),
        shot_zone_basic=_str_or_none(row_map.get("SHOT_ZONE_BASIC")),
        shot_zone_area=_str_or_none(row_map.get("SHOT_ZONE_AREA")),
        shot_zone_range=_str_or_none(row_map.get("SHOT_ZONE_RANGE")),
        action_type=_str_or_none(row_map.get("ACTION_TYPE")),
        made=bool(made_flag) if made_flag is not None else False,
    )


def chunked(items: list[Any], size: int) -> list[list[Any]]:
    """Split ``items`` into consecutive chunks of at most ``size`` elements."""
    return [items[i : i + size] for i in range(0, len(items), size)]


def _expire_written_instances(db: AsyncSession, models: tuple[type, ...]) -> None:
    """Expire already-loaded instances of ``models`` still in this session.

    The bulk upsert helpers above write via raw Core ``INSERT ... ON
    CONFLICT``, which never touches the ORM identity map -- so an instance
    of one of these tables already loaded into this session (by an earlier
    phase sharing the same session, or by a caller/test holding a
    reference) would otherwise keep serving its pre-upsert attribute values
    indefinitely: this app's sessionmaker uses ``expire_on_commit=False``
    (``app/utils/db_async.py``), so a commit alone does not clear that
    staleness.

    This is deliberately **not** ``db.expire_all()``. Expiring the whole
    session marks every attribute of every loaded object -- including
    primary keys, which are not exempt -- as needing a reload on next
    access. In an async session that reload can only happen inside an
    awaited call; a later *synchronous* attribute read on some unrelated,
    already-loaded object (a ``PlayerMaster``, a ``SummerLeagueGame``, a
    caller's own row -- anything sharing this session, not just the tables
    this module writes) then raises
    ``sqlalchemy.exc.MissingGreenlet: greenlet_spawn has not been called``
    instead of quietly refreshing. Scoping expiry to exactly the mapped
    classes these bulk writes touch keeps that blast radius to the rows
    this module is actually responsible for.

    Args:
        db: Active database session.
        models: Mapped classes to expire matching identity-map entries for
            (e.g. ``(SummerLeagueSourcePlayer, SummerLeagueShotEvent)``).
    """
    for obj in list(db.identity_map.values()):
        if isinstance(obj, models):
            db.expire(obj)


async def _bulk_upsert_source_players(
    db: AsyncSession,
    identities: dict[str, ParsedPlayerGamelogRow],
    *,
    year: int,
) -> dict[str, _SourcePlayerRef]:
    """Bulk upsert ``SummerLeagueSourcePlayer`` rows for a batch's identities.

    Mirrors ``_upsert_source_player``'s semantics -- a new row is created
    UNRESOLVED with ``first_seen_year``/``last_seen_year`` seeded from
    ``year``; an existing row keeps its resolution fields
    (``canonical_player_id``, ``resolution_status``, etc.) untouched and only
    has its name/seen-year bounds refreshed -- but issues one chunked
    ``INSERT ... ON CONFLICT ... RETURNING`` per call instead of a
    SELECT-then-write per row.

    Args:
        db: Active database session.
        identities: Distinct ``nba_stats_person_id`` -> parsed identity for
            this batch (the caller keeps only the last occurrence per id,
            matching the row loop this replaces).
        year: Competition year, used to seed/extend first/last_seen_year.

    Returns:
        Map of ``nba_stats_person_id`` -> resolved ``(id,
        canonical_player_id)`` for every identity supplied. Empty when
        ``identities`` is empty.
    """
    if not identities:
        return {}
    now = _utc_now_naive()
    table = getattr(SummerLeagueSourcePlayer, "__table__")
    refs: dict[str, _SourcePlayerRef] = {}
    for chunk in chunked(list(identities.values()), BULK_UPSERT_CHUNK_SIZE):
        values = [
            {
                "nba_stats_person_id": row.nba_stats_person_id,
                "raw_player_name": row.raw_player_name,
                "normalized_name": _normalized_name_key(row.raw_player_name),
                "first_seen_year": year,
                "last_seen_year": year,
                "canonical_player_id": None,
                "resolution_status": SummerLeagueResolutionStatus.UNRESOLVED,
                "resolution_confidence": None,
                "resolution_candidates": None,
                "resolved_at": None,
                "resolved_by": None,
                "created_at": now,
                "updated_at": now,
            }
            for row in chunk
        ]
        insert_stmt = insert(table).values(values)
        stmt = insert_stmt.on_conflict_do_update(
            index_elements=["nba_stats_person_id"],
            set_={
                "raw_player_name": insert_stmt.excluded.raw_player_name,
                "normalized_name": insert_stmt.excluded.normalized_name,
                "first_seen_year": func.least(
                    table.c.first_seen_year, insert_stmt.excluded.first_seen_year
                ),
                "last_seen_year": func.greatest(
                    table.c.last_seen_year, insert_stmt.excluded.last_seen_year
                ),
                "updated_at": insert_stmt.excluded.updated_at,
            },
        ).returning(
            table.c.nba_stats_person_id,
            table.c.id,
            table.c.canonical_player_id,
        )
        result = await db.execute(stmt)
        for person_id, source_id, canonical_id in result.all():
            refs[person_id] = _SourcePlayerRef(
                id=source_id, canonical_player_id=canonical_id
            )
    return refs


async def _bulk_upsert(
    db: AsyncSession,
    table: Any,
    rows: list[dict[str, Any]],
    *,
    index_elements: list[str],
    mutable_columns: tuple[str, ...],
) -> None:
    """Chunked ``INSERT ... ON CONFLICT ... DO UPDATE`` for one event table.

    Shared by :func:`_bulk_upsert_shot_events` and
    :func:`_bulk_upsert_pbp_events` -- both replace a SELECT-then-write
    per-event helper with the same chunk-and-upsert shape, differing only in
    the target table, conflict key, and mutable column set.

    Args:
        db: Active database session.
        table: SQLAlchemy Core table (``getattr(Model, "__table__")``).
        rows: Fully-built column dicts, already deduped last-row-wins per
            conflict key by the caller. A no-op when empty.
        index_elements: Unique-constraint columns identifying one row.
        mutable_columns: Columns to overwrite from the incoming row on
            conflict.
    """
    if not rows:
        return
    for chunk in chunked(rows, BULK_UPSERT_CHUNK_SIZE):
        stmt = insert(table).values(chunk)
        stmt = stmt.on_conflict_do_update(
            index_elements=index_elements,
            set_={col: getattr(stmt.excluded, col) for col in mutable_columns},
        )
        await db.execute(stmt)


async def _bulk_upsert_shot_events(
    db: AsyncSession,
    rows: list[dict[str, Any]],
) -> None:
    """Chunked ``INSERT ... ON CONFLICT`` for shot events.

    Keyed on the same ``(nba_stats_game_id, nba_stats_game_event_id)`` pair
    as ``uq_summer_league_shot_events_game_event``, matching the
    SELECT-then-write ``_upsert_shot_event`` helper this replaces.

    Args:
        db: Active database session.
        rows: Fully-built column dicts (one per shot, already deduped
            last-row-wins per key by the caller) matching
            ``SummerLeagueShotEvent`` columns. A no-op when empty.
    """
    await _bulk_upsert(
        db,
        getattr(SummerLeagueShotEvent, "__table__"),
        rows,
        index_elements=["nba_stats_game_id", "nba_stats_game_event_id"],
        mutable_columns=(
            "game_id",
            "competition_id",
            "team_entry_id",
            "source_player_id",
            "player_id",
            "nba_stats_person_id",
            "period",
            "minutes_remaining",
            "seconds_remaining",
            "loc_x",
            "loc_y",
            "shot_distance",
            "shot_type",
            "shot_zone_basic",
            "shot_zone_area",
            "shot_zone_range",
            "action_type",
            "made",
            "updated_at",
        ),
    )


def _parse_pbp_row(row_map: dict[str, Any]) -> ParsedPBPEvent | None:
    game_id = str(row_map.get("GAME_ID") or "")
    event_num_raw = row_map.get("EVENTNUM")
    if not game_id or event_num_raw is None:
        return None
    event_num = _int_or_none(event_num_raw)
    if event_num is None:
        return None

    home_score, away_score = _parse_score(row_map.get("SCORE"))

    # Combine non-empty descriptions from the three description columns.
    desc_parts = [
        _str_or_none(row_map.get("HOMEDESCRIPTION")),
        _str_or_none(row_map.get("NEUTRALDESCRIPTION")),
        _str_or_none(row_map.get("VISITORDESCRIPTION")),
    ]
    description = " ".join(p for p in desc_parts if p) or None

    # Raw NBA person IDs (may be 0 or None for events with no actor).
    person1_raw = row_map.get("PLAYER1_ID")
    person2_raw = row_map.get("PLAYER2_ID")
    person3_raw = row_map.get("PLAYER3_ID")

    return ParsedPBPEvent(
        nba_stats_game_id=game_id,
        event_num=event_num,
        period=_int_or_none(row_map.get("PERIOD")),
        clock=_str_or_none(row_map.get("PCTIMESTRING")),
        event_msg_type=_int_or_none(row_map.get("EVENTMSGTYPE")),
        home_score=home_score,
        away_score=away_score,
        score_margin=_parse_score_margin(row_map.get("SCOREMARGIN")),
        person1_nba_id=_nba_person_id_or_none(person1_raw),
        person2_nba_id=_nba_person_id_or_none(person2_raw),
        person3_nba_id=_nba_person_id_or_none(person3_raw),
        description=description,
    )


async def _preload_actor_ids(
    db: AsyncSession,
    nba_person_ids: set[str],
) -> dict[str, int | None]:
    """Bulk-resolve NBA Stats person IDs to canonical player FKs via source players.

    Mirrors ``_resolve_actor_id``'s lookup-only semantics (never creates a
    source player) but issues one chunked ``SELECT ... WHERE
    nba_stats_person_id IN (...)`` for the whole batch instead of up to
    three SELECTs per PBP row.

    Args:
        db: Active database session.
        nba_person_ids: Distinct non-empty actor IDs referenced anywhere in
            the batch's person1/person2/person3 columns.

    Returns:
        Map of ``nba_stats_person_id`` -> ``canonical_player_id`` (may be
        ``None`` for a resolved-but-unlinked source player). An id with no
        matching source player row is simply absent from the map; callers
        should use ``.get(id)`` and treat a missing key the same as
        ``_resolve_actor_id``'s ``None`` return.
    """
    if not nba_person_ids:
        return {}
    mapping: dict[str, int | None] = {}
    for chunk in chunked(sorted(nba_person_ids), BULK_UPSERT_CHUNK_SIZE):
        result = await db.execute(
            select(  # type: ignore[call-overload]
                SummerLeagueSourcePlayer.nba_stats_person_id,
                SummerLeagueSourcePlayer.canonical_player_id,
            ).where(
                SummerLeagueSourcePlayer.nba_stats_person_id.in_(chunk)  # type: ignore[attr-defined]
            )
        )
        for person_id, canonical_id in result.all():
            mapping[person_id] = canonical_id
    return mapping


async def _bulk_upsert_pbp_events(
    db: AsyncSession,
    rows: list[dict[str, Any]],
) -> None:
    """Chunked ``INSERT ... ON CONFLICT`` for play-by-play events.

    Keyed on the same ``(nba_stats_game_id, event_num)`` pair as
    ``uq_summer_league_pbp_events_game_event_num``, matching the
    SELECT-then-write ``_upsert_pbp_event`` helper this replaces.

    Args:
        db: Active database session.
        rows: Fully-built column dicts (one per event, already deduped
            last-row-wins per key by the caller) matching
            ``SummerLeaguePlayByPlayEvent`` columns. A no-op when empty.
    """
    await _bulk_upsert(
        db,
        getattr(SummerLeaguePlayByPlayEvent, "__table__"),
        rows,
        index_elements=["nba_stats_game_id", "event_num"],
        mutable_columns=(
            "game_id",
            "competition_id",
            "period",
            "clock",
            "event_msg_type",
            "home_score",
            "away_score",
            "score_margin",
            "person1_nba_id",
            "person1_id",
            "person2_nba_id",
            "person2_id",
            "person3_nba_id",
            "person3_id",
            "description",
            "updated_at",
        ),
    )


async def _pbp_raw_files_by_game(
    db: AsyncSession,
    *,
    raw_run_id: int | None,
) -> dict[str, SummerLeagueRawFile]:
    """Return playbyplayv2 raw file records keyed by nba_stats_game_id."""
    if raw_run_id is None:
        return {}
    result = await db.execute(
        select(SummerLeagueRawFile).where(
            SummerLeagueRawFile.raw_run_id == raw_run_id,  # type: ignore[arg-type]
            SummerLeagueRawFile.endpoint == "playbyplayv2",  # type: ignore[arg-type]
        )
    )
    return {
        file.game_id: file
        for file in result.scalars().all()
        if file.game_id is not None
    }


async def _shot_raw_files_by_game(
    db: AsyncSession,
    *,
    raw_run_id: int | None,
) -> dict[str, SummerLeagueRawFile]:
    """Return shotchartdetail raw file records keyed by nba_stats_game_id."""
    if raw_run_id is None:
        return {}
    result = await db.execute(
        select(SummerLeagueRawFile).where(
            SummerLeagueRawFile.raw_run_id == raw_run_id,  # type: ignore[arg-type]
            SummerLeagueRawFile.endpoint == "shotchartdetail",  # type: ignore[arg-type]
        )
    )
    return {
        file.game_id: file
        for file in result.scalars().all()
        if file.game_id is not None
    }


def _parse_score(value: object) -> tuple[int | None, int | None]:
    """Parse a PBP SCORE string (e.g. '5 - 3') into (home_score, away_score)."""
    if not value or not isinstance(value, str):
        return None, None
    parts = value.split(" - ")
    if len(parts) != 2:
        return None, None
    return _int_or_none(parts[0].strip()), _int_or_none(parts[1].strip())


def _parse_score_margin(value: object) -> int | None:
    """Parse a PBP SCOREMARGIN value ('TIE', numeric string, or None)."""
    if value is None or value == "":
        return None
    if isinstance(value, str) and value.upper() == "TIE":
        return 0
    return _int_or_none(value)


def _nba_person_id_or_none(value: object) -> str | None:
    """Return a person ID string, or None for zero/null/empty values."""
    if value is None or value == "" or value == 0:
        return None
    raw = str(value).strip()
    if raw in ("0", ""):
        return None
    return raw


def _read_payload(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text())
    return payload if isinstance(payload, dict) else {}


def _row_map(headers: list[str], row: list[Any]) -> dict[str, Any]:
    return {
        header: row[index] if index < len(row) else None
        for index, header in enumerate(headers)
    }


def _display_name(year: int, venue_slug: str) -> str:
    return f"{year} {venue_slug.replace('_', ' ').title()} Summer League"


def _parse_date(value: object) -> date | None:
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return date.fromisoformat(str(value))


def _source_player_id(row_map: dict[str, Any]) -> str:
    return str(
        row_map.get("PERSON_ID")
        or row_map.get("PLAYER_ID")
        or row_map.get("NBA_PERSON_ID")
        or ""
    )


def _source_player_name(row_map: dict[str, Any]) -> str:
    return str(
        row_map.get("PLAYER_NAME")
        or row_map.get("PLAYER_NAME_I")
        or row_map.get("DISPLAY_FIRST_LAST")
        or ""
    )


def _str_or_none(value: object) -> str | None:
    return str(value) if value not in (None, "") else None


def _int_or_none(value: object) -> int | None:
    if value in (None, ""):
        return None
    return int(float(str(value)))


def _float_or_none(value: object) -> float | None:
    if value in (None, ""):
        return None
    return float(str(value))


def _utc_now_naive() -> datetime:
    return datetime.utcnow()
