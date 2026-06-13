"""Summer League raw audit and normalized product schemas."""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    Column,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class SummerLeagueRawRunStatus(str, Enum):
    """Status of auditing one raw Summer League manifest/run."""

    PENDING = "PENDING"
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class SummerLeagueRawFileStatus(str, Enum):
    """File-level audit and parse status for raw Summer League snapshots."""

    PRESENT = "PRESENT"
    MISSING = "MISSING"
    EMPTY = "EMPTY"
    PARSED = "PARSED"
    PARSE_FAILED = "PARSE_FAILED"
    SKIPPED = "SKIPPED"


class SummerLeagueDataQuality(str, Enum):
    """User-facing Summer League competition or game data quality."""

    FULL = "full"
    PARTIAL = "partial"
    BOX_ONLY = "box_only"
    RAW_ONLY = "raw_only"


class SummerLeagueGameStatus(str, Enum):
    """Normalized Summer League game status."""

    SCHEDULED = "scheduled"
    FINAL = "final"
    UNKNOWN = "unknown"


class SummerLeagueResolutionStatus(str, Enum):
    """Source-player to canonical-player resolution state."""

    UNRESOLVED = "UNRESOLVED"
    EXTERNAL_ID = "EXTERNAL_ID"
    EXACT = "EXACT"
    ALIAS = "ALIAS"
    FUZZY = "FUZZY"
    VECTOR_CANDIDATE = "VECTOR_CANDIDATE"
    MANUAL = "MANUAL"
    STUB = "STUB"


class SummerLeagueRawRun(SQLModel, table=True):  # type: ignore[call-arg]
    """One audited Summer League raw scrape manifest."""

    __tablename__ = "summer_league_raw_runs"
    __table_args__ = (
        UniqueConstraint(
            "year",
            "league_id",
            "manifest_path",
            name="uq_summer_league_raw_runs_year_league_manifest",
        ),
        Index("ix_summer_league_raw_runs_year_league", "year", "league_id"),
        Index("ix_summer_league_raw_runs_status", "status"),
        CheckConstraint("year >= 2000", name="ck_summer_league_raw_runs_year"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    year: int = Field(nullable=False)
    league_id: str = Field(nullable=False)
    venue_slug: str = Field(nullable=False)
    status: SummerLeagueRawRunStatus = Field(
        default=SummerLeagueRawRunStatus.PENDING,
        sa_column=Column(
            SAEnum(
                SummerLeagueRawRunStatus,
                name="summer_league_raw_run_status_enum",
            ),
            nullable=False,
            server_default=SummerLeagueRawRunStatus.PENDING.value,
        ),
    )
    started_at: Optional[datetime] = Field(default=None)
    finished_at: Optional[datetime] = Field(default=None)
    team_gamelog_rows: int = Field(default=0, nullable=False)
    player_gamelog_rows: int = Field(default=0, nullable=False)
    game_count: int = Field(default=0, nullable=False)
    error_count: int = Field(default=0, nullable=False)
    manifest_path: str = Field(nullable=False)
    manifest_sha256: Optional[str] = Field(default=None)
    s3_manifest_key: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
    updated_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)


class SummerLeagueRawFile(SQLModel, table=True):  # type: ignore[call-arg]
    """One audited Summer League raw JSON snapshot file."""

    __tablename__ = "summer_league_raw_files"
    __table_args__ = (
        UniqueConstraint(
            "raw_run_id",
            "endpoint",
            "game_id",
            name="uq_summer_league_raw_files_run_endpoint_game",
        ),
        UniqueConstraint(
            "relative_path",
            name="uq_summer_league_raw_files_relative_path",
        ),
        Index(
            "ix_summer_league_raw_files_year_league_endpoint",
            "year",
            "league_id",
            "endpoint",
        ),
        Index("ix_summer_league_raw_files_game_id", "game_id"),
        Index("ix_summer_league_raw_files_parse_status", "parse_status"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    raw_run_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("summer_league_raw_runs.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    year: int = Field(nullable=False)
    league_id: str = Field(nullable=False)
    endpoint: str = Field(nullable=False)
    game_id: Optional[str] = Field(default=None)
    relative_path: str = Field(nullable=False)
    s3_key: Optional[str] = Field(default=None)
    sha256: Optional[str] = Field(default=None)
    byte_size: Optional[int] = Field(default=None)
    row_count: Optional[int] = Field(default=None)
    parse_status: SummerLeagueRawFileStatus = Field(
        default=SummerLeagueRawFileStatus.PRESENT,
        sa_column=Column(
            SAEnum(
                SummerLeagueRawFileStatus,
                name="summer_league_raw_file_status_enum",
            ),
            nullable=False,
            server_default=SummerLeagueRawFileStatus.PRESENT.value,
        ),
    )
    parse_error: Optional[str] = Field(default=None, sa_column=Column(Text))
    fetched_at: Optional[datetime] = Field(default=None)
    audited_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
    updated_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)


class SummerLeagueCompetition(SQLModel, table=True):  # type: ignore[call-arg]
    """One normalized Summer League competition for a year and NBA.com LeagueID."""

    __tablename__ = "summer_league_competitions"
    __table_args__ = (
        UniqueConstraint(
            "year",
            "league_id",
            name="uq_summer_league_competitions_year_league",
        ),
        Index("ix_summer_league_competitions_year_venue", "year", "venue_slug"),
        CheckConstraint("year >= 2000", name="ck_summer_league_competitions_year"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    year: int = Field(nullable=False)
    league_id: str = Field(nullable=False)
    venue_slug: str = Field(nullable=False)
    display_name: str = Field(nullable=False)
    starts_on: Optional[date] = Field(default=None)
    ends_on: Optional[date] = Field(default=None)
    data_quality: SummerLeagueDataQuality = Field(
        default=SummerLeagueDataQuality.RAW_ONLY,
        sa_column=Column(
            SAEnum(
                SummerLeagueDataQuality,
                name="summer_league_data_quality_enum",
                values_callable=lambda enum_cls: [item.value for item in enum_cls],
            ),
            nullable=False,
            server_default=SummerLeagueDataQuality.RAW_ONLY.value,
        ),
    )
    pbp_available: bool = Field(default=False, nullable=False)
    shotchart_available: bool = Field(default=False, nullable=False)
    raw_run_id: Optional[int] = Field(
        default=None, foreign_key="summer_league_raw_runs.id"
    )
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
    updated_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)


class SummerLeagueTeamEntry(SQLModel, table=True):  # type: ignore[call-arg]
    """One source team entry in a Summer League competition."""

    __tablename__ = "summer_league_team_entries"
    __table_args__ = (
        UniqueConstraint(
            "competition_id",
            "nba_stats_team_id",
            name="uq_summer_league_team_entries_competition_source_team",
        ),
        Index("ix_summer_league_team_entries_nba_team_id", "nba_team_id"),
        Index(
            "ix_summer_league_team_entries_competition_team_slug",
            "competition_id",
            "team_slug",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    competition_id: int = Field(foreign_key="summer_league_competitions.id")
    nba_team_id: Optional[int] = Field(default=None, foreign_key="nba_teams.id")
    nba_stats_team_id: str = Field(nullable=False)
    raw_team_name: str = Field(nullable=False)
    raw_team_abbreviation: Optional[str] = Field(default=None)
    team_slug: str = Field(nullable=False)
    wins: Optional[int] = Field(default=None)
    losses: Optional[int] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
    updated_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)


class SummerLeagueGame(SQLModel, table=True):  # type: ignore[call-arg]
    """One normalized NBA.com Summer League game."""

    __tablename__ = "summer_league_games"
    __table_args__ = (
        UniqueConstraint(
            "nba_stats_game_id",
            name="uq_summer_league_games_nba_stats_game_id",
        ),
        Index("ix_summer_league_games_competition_date", "competition_id", "game_date"),
        Index("ix_summer_league_games_home_team_entry_id", "home_team_entry_id"),
        Index("ix_summer_league_games_away_team_entry_id", "away_team_entry_id"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    competition_id: int = Field(foreign_key="summer_league_competitions.id")
    nba_stats_game_id: str = Field(nullable=False)
    game_date: Optional[date] = Field(default=None)
    home_team_entry_id: Optional[int] = Field(
        default=None,
        foreign_key="summer_league_team_entries.id",
    )
    away_team_entry_id: Optional[int] = Field(
        default=None,
        foreign_key="summer_league_team_entries.id",
    )
    home_score: Optional[int] = Field(default=None)
    away_score: Optional[int] = Field(default=None)
    status: SummerLeagueGameStatus = Field(
        default=SummerLeagueGameStatus.UNKNOWN,
        sa_column=Column(
            SAEnum(
                SummerLeagueGameStatus,
                name="summer_league_game_status_enum",
                values_callable=lambda enum_cls: [item.value for item in enum_cls],
            ),
            nullable=False,
            server_default=SummerLeagueGameStatus.UNKNOWN.value,
        ),
    )
    source_quality: SummerLeagueDataQuality = Field(
        default=SummerLeagueDataQuality.RAW_ONLY,
        sa_column=Column(
            SAEnum(
                SummerLeagueDataQuality,
                name="summer_league_data_quality_enum",
                values_callable=lambda enum_cls: [item.value for item in enum_cls],
            ),
            nullable=False,
            server_default=SummerLeagueDataQuality.RAW_ONLY.value,
        ),
    )
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
    updated_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)


class SummerLeagueSourcePlayer(SQLModel, table=True):  # type: ignore[call-arg]
    """One NBA.com source player identity before and after canonical resolution."""

    __tablename__ = "summer_league_source_players"
    __table_args__ = (
        UniqueConstraint(
            "nba_stats_person_id",
            name="uq_summer_league_source_players_nba_stats_person_id",
        ),
        Index(
            "ix_summer_league_source_players_canonical_player_id",
            "canonical_player_id",
        ),
        Index("ix_summer_league_source_players_normalized_name", "normalized_name"),
        Index(
            "ix_summer_league_source_players_resolution_status",
            "resolution_status",
        ),
        CheckConstraint(
            "resolution_confidence IS NULL OR "
            "(resolution_confidence >= 0 AND resolution_confidence <= 1)",
            name="ck_summer_league_source_players_resolution_confidence",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    nba_stats_person_id: str = Field(nullable=False)
    raw_player_name: str = Field(nullable=False)
    normalized_name: str = Field(nullable=False)
    first_seen_year: Optional[int] = Field(default=None)
    last_seen_year: Optional[int] = Field(default=None)
    canonical_player_id: Optional[int] = Field(
        default=None, foreign_key="players_master.id"
    )
    resolution_status: SummerLeagueResolutionStatus = Field(
        default=SummerLeagueResolutionStatus.UNRESOLVED,
        sa_column=Column(
            SAEnum(
                SummerLeagueResolutionStatus,
                name="summer_league_resolution_status_enum",
            ),
            nullable=False,
            server_default=SummerLeagueResolutionStatus.UNRESOLVED.value,
        ),
    )
    resolution_confidence: Optional[float] = Field(default=None)
    resolution_candidates: Optional[list[dict[str, object]]] = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
    )
    resolved_at: Optional[datetime] = Field(default=None)
    resolved_by: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
    updated_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)


class SummerLeagueTeamGameLog(SQLModel, table=True):  # type: ignore[call-arg]
    """One normalized Summer League team box-score line."""

    __tablename__ = "summer_league_team_game_logs"
    __table_args__ = (
        UniqueConstraint(
            "game_id",
            "team_entry_id",
            name="uq_summer_league_team_game_logs_game_team",
        ),
        Index(
            "ix_summer_league_team_game_logs_competition_team",
            "competition_id",
            "team_entry_id",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    competition_id: int = Field(foreign_key="summer_league_competitions.id")
    game_id: int = Field(foreign_key="summer_league_games.id")
    team_entry_id: int = Field(foreign_key="summer_league_team_entries.id")
    minutes: Optional[int] = Field(default=None)
    pts: Optional[int] = Field(default=None)
    fgm: Optional[int] = Field(default=None)
    fga: Optional[int] = Field(default=None)
    fg_pct: Optional[float] = Field(default=None)
    fg3m: Optional[int] = Field(default=None)
    fg3a: Optional[int] = Field(default=None)
    fg3_pct: Optional[float] = Field(default=None)
    ftm: Optional[int] = Field(default=None)
    fta: Optional[int] = Field(default=None)
    ft_pct: Optional[float] = Field(default=None)
    oreb: Optional[int] = Field(default=None)
    dreb: Optional[int] = Field(default=None)
    reb: Optional[int] = Field(default=None)
    ast: Optional[int] = Field(default=None)
    stl: Optional[int] = Field(default=None)
    blk: Optional[int] = Field(default=None)
    tov: Optional[int] = Field(default=None)
    pf: Optional[int] = Field(default=None)
    plus_minus: Optional[int] = Field(default=None)
    off_rating: Optional[float] = Field(default=None)
    def_rating: Optional[float] = Field(default=None)
    net_rating: Optional[float] = Field(default=None)
    ast_pct: Optional[float] = Field(default=None)
    reb_pct: Optional[float] = Field(default=None)
    efg_pct: Optional[float] = Field(default=None)
    ts_pct: Optional[float] = Field(default=None)
    pace: Optional[float] = Field(default=None)
    source_endpoint: str = Field(default="boxscoretraditionalv2", nullable=False)
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
    updated_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)


class SummerLeaguePlayerGameLog(SQLModel, table=True):  # type: ignore[call-arg]
    """One normalized Summer League player box-score line."""

    __tablename__ = "summer_league_player_game_logs"
    __table_args__ = (
        UniqueConstraint(
            "game_id",
            "nba_stats_person_id",
            "team_entry_id",
            name="uq_summer_league_player_game_logs_game_person_team",
        ),
        Index(
            "ix_summer_league_player_game_logs_competition_player",
            "competition_id",
            "player_id",
        ),
        Index("ix_summer_league_player_game_logs_player_id", "player_id"),
        Index(
            "ix_summer_league_player_game_logs_competition_source_player",
            "competition_id",
            "source_player_id",
        ),
        Index("ix_summer_league_player_game_logs_team_entry_id", "team_entry_id"),
        Index(
            "ix_summer_league_player_game_logs_nba_stats_person_id",
            "nba_stats_person_id",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    competition_id: int = Field(foreign_key="summer_league_competitions.id")
    game_id: int = Field(foreign_key="summer_league_games.id")
    team_entry_id: int = Field(foreign_key="summer_league_team_entries.id")
    source_player_id: int = Field(foreign_key="summer_league_source_players.id")
    player_id: Optional[int] = Field(default=None, foreign_key="players_master.id")
    nba_stats_person_id: str = Field(nullable=False)
    raw_player_name: str = Field(nullable=False)
    starter_position: Optional[str] = Field(default=None)
    comment: Optional[str] = Field(default=None)
    minutes_seconds: Optional[int] = Field(default=None)
    pts: Optional[int] = Field(default=None)
    fgm: Optional[int] = Field(default=None)
    fga: Optional[int] = Field(default=None)
    fg_pct: Optional[float] = Field(default=None)
    fg3m: Optional[int] = Field(default=None)
    fg3a: Optional[int] = Field(default=None)
    fg3_pct: Optional[float] = Field(default=None)
    ftm: Optional[int] = Field(default=None)
    fta: Optional[int] = Field(default=None)
    ft_pct: Optional[float] = Field(default=None)
    oreb: Optional[int] = Field(default=None)
    dreb: Optional[int] = Field(default=None)
    reb: Optional[int] = Field(default=None)
    ast: Optional[int] = Field(default=None)
    stl: Optional[int] = Field(default=None)
    blk: Optional[int] = Field(default=None)
    tov: Optional[int] = Field(default=None)
    pf: Optional[int] = Field(default=None)
    plus_minus: Optional[int] = Field(default=None)
    off_rating: Optional[float] = Field(default=None)
    def_rating: Optional[float] = Field(default=None)
    net_rating: Optional[float] = Field(default=None)
    ast_pct: Optional[float] = Field(default=None)
    oreb_pct: Optional[float] = Field(default=None)
    dreb_pct: Optional[float] = Field(default=None)
    reb_pct: Optional[float] = Field(default=None)
    tm_tov_pct: Optional[float] = Field(default=None)
    efg_pct: Optional[float] = Field(default=None)
    ts_pct: Optional[float] = Field(default=None)
    usg_pct: Optional[float] = Field(default=None)
    pace: Optional[float] = Field(default=None)
    pie: Optional[float] = Field(default=None)
    pct_fga_2pt: Optional[float] = Field(default=None)
    pct_fga_3pt: Optional[float] = Field(default=None)
    pct_pts_2pt: Optional[float] = Field(default=None)
    pct_pts_3pt: Optional[float] = Field(default=None)
    pct_pts_ft: Optional[float] = Field(default=None)
    source_endpoint: str = Field(default="boxscoretraditionalv2", nullable=False)
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
    updated_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
