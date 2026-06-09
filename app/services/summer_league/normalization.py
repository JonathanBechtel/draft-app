"""Normalize audited Summer League raw data into product tables."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeagueDataQuality,
    SummerLeagueGame,
    SummerLeagueGameStatus,
    SummerLeaguePlayerGameLog,
    SummerLeagueRawFile,
    SummerLeagueRawFileStatus,
    SummerLeagueRawRun,
    SummerLeagueResolutionStatus,
    SummerLeagueSourcePlayer,
    SummerLeagueTeamEntry,
    SummerLeagueTeamGameLog,
)
from app.services.player_mention_service import _normalized_name_key
from app.services.summer_league.nba_stats_client import (
    NBAStatsResultSet,
    extract_result_sets,
)


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
) -> SummerLeagueNormalizationReport:
    """Normalize competition, teams, games, and team game logs for one slice."""
    raw_run = await _get_raw_run(db, year=year, league_id=league_id)
    if raw_run.id is None:
        raise RuntimeError("Raw run id was not populated")
    raw_files = await _get_raw_files(db, raw_run_id=raw_run.id)
    quality = _competition_quality(raw_run, raw_files)
    competition = await _upsert_competition(db, raw_run, quality)
    await db.flush()
    if competition.id is None:
        raise RuntimeError("Competition id was not populated after flush")

    team_gamelog_rows = parse_team_gamelog(
        raw_root / f"{year}/{league_id}/leaguegamelog_team.json"
    )
    teams_by_source_id: dict[str, SummerLeagueTeamEntry] = {}
    for row in team_gamelog_rows:
        team = await _upsert_team_entry(db, competition.id, row)
        await db.flush()
        teams_by_source_id[row.nba_stats_team_id] = team

    game_rows = _group_game_rows(team_gamelog_rows)
    games_by_source_id: dict[str, SummerLeagueGame] = {}
    for game_id, rows in game_rows.items():
        game = await _upsert_game(
            db,
            competition.id,
            game_id,
            rows,
            teams_by_source_id,
            quality,
        )
        await db.flush()
        games_by_source_id[game_id] = game

    team_log_count = 0
    for box_row in parse_team_box_rows(raw_root / f"{year}/{league_id}"):
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
        team_log_count += 1

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


async def normalize_player_game_logs(
    db: AsyncSession,
    *,
    year: int,
    league_id: str,
    raw_root: Path,
) -> SummerLeaguePlayerLogReport:
    """Normalize source players and player game logs for one Summer League slice."""
    competition = await _get_competition(db, year=year, league_id=league_id)
    if competition.id is None:
        raise RuntimeError("Competition id was not populated")

    season_dir = raw_root / f"{year}/{league_id}"
    source_player_ids: set[str] = set()
    for gamelog_row in parse_player_gamelog(season_dir / "leaguegamelog_player.json"):
        await _upsert_source_player(db, gamelog_row, year=year)
        source_player_ids.add(gamelog_row.nba_stats_person_id)

    await db.flush()
    games_by_source_id = await _games_by_source_id(db, competition.id)
    teams_by_source_id = await _teams_by_source_id(db, competition.id)

    upserted_logs = 0
    skipped_logs = 0
    for box_row in parse_player_box_rows(season_dir):
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
            continue

        await _upsert_player_game_log(
            db,
            competition.id,
            game.id,
            team.id,
            source_player,
            box_row,
        )
        upserted_logs += 1

    await db.flush()
    return SummerLeaguePlayerLogReport(
        year=year,
        league_id=league_id,
        competition_id=competition.id,
        source_players_upserted=len(source_player_ids),
        player_game_logs_upserted=upserted_logs,
        player_game_logs_skipped=skipped_logs,
    )


def parse_team_gamelog(path: Path) -> list[ParsedTeamGamelogRow]:
    """Parse source team gamelog rows."""
    payload = _read_payload(path)
    result_sets = extract_result_sets(payload)
    if not result_sets:
        return []
    result_set = result_sets[0]
    return [_parse_team_gamelog_row(result_set, row) for row in result_set.rows]


def parse_team_box_rows(season_dir: Path) -> list[ParsedTeamBoxRow]:
    """Parse team box-score rows from all game directories in one season."""
    rows: list[ParsedTeamBoxRow] = []
    games_root = season_dir / "games"
    if not games_root.exists():
        return rows
    for game_dir in sorted(path for path in games_root.iterdir() if path.is_dir()):
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


def parse_player_box_rows(season_dir: Path) -> list[ParsedPlayerBoxRow]:
    """Parse player box-score rows from all game directories in one season."""
    rows: list[ParsedPlayerBoxRow] = []
    games_root = season_dir / "games"
    if not games_root.exists():
        return rows
    for game_dir in sorted(path for path in games_root.iterdir() if path.is_dir()):
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


async def _upsert_game(
    db: AsyncSession,
    competition_id: int,
    game_id: str,
    rows: list[ParsedTeamGamelogRow],
    teams_by_source_id: dict[str, SummerLeagueTeamEntry],
    quality: SummerLeagueDataQuality,
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
    row.status = SummerLeagueGameStatus.FINAL
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


async def _upsert_team_game_log(
    db: AsyncSession,
    competition_id: int,
    game_id: int,
    team_entry_id: int,
    box_row: ParsedTeamBoxRow,
) -> SummerLeagueTeamGameLog:
    result = await db.execute(
        select(SummerLeagueTeamGameLog).where(
            SummerLeagueTeamGameLog.game_id == game_id,  # type: ignore[arg-type]
            SummerLeagueTeamGameLog.team_entry_id == team_entry_id,  # type: ignore[arg-type]
        )
    )
    row = result.scalar_one_or_none()
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
    row.source_endpoint = "boxscoretraditionalv2"
    row.updated_at = _utc_now_naive()
    return row


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


async def _upsert_player_game_log(
    db: AsyncSession,
    competition_id: int,
    game_id: int,
    team_entry_id: int,
    source_player: SummerLeagueSourcePlayer,
    box_row: ParsedPlayerBoxRow,
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
    row.player_id = source_player.canonical_player_id
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
    if advanced is None:
        return traditional
    values = traditional.__dict__.copy()
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
