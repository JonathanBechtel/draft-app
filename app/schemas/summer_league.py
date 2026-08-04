"""Summer League raw audit and normalized product schemas."""

# discipline: file-size one spoke table family per module; growth is the additive team_program_id retarget

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
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from app.schemas.player_affiliation import AffiliationStatus

# The event-grain tables moved to their own module (#681); re-exported so the
# existing import sites keep working. See app/schemas/summer_league_events.py.
from app.schemas.summer_league_events import (  # noqa: F401
    SummerLeaguePlayByPlayEvent,
    SummerLeagueShotEvent,
)


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
    IN_PROGRESS = "in_progress"
    FINAL = "final"
    UNKNOWN = "unknown"
    # Terminal, non-Live statuses (fix #4, follow-up to #529/#530/fix #3): a
    # postponed/canceled game will never tip, so it must be excluded by every
    # consumer that filters on this column -- not re-derived per-consumer from
    # ``status_text`` (see `scoreboard_ingest.map_game_status`,
    # `live_ingestion._LIVE_STATUSES`, `normalization.resolve_game_status`,
    # `event_desk.registry._to_generic_status`).
    POSTPONED = "postponed"
    CANCELED = "canceled"


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


class SummerLeagueReviewStatus(str, Enum):
    """Lifecycle status for Summer League player-resolution review rows."""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    STUB_CREATED = "STUB_CREATED"


class SummerLeagueIngestionRun(SQLModel, table=True):  # type: ignore[call-arg]
    """One audited Summer League raw scrape manifest.

    An ingestion run / document batch in backbone terms (journey-graph §10).
    """

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


class SummerLeagueSourceDocument(SQLModel, table=True):  # type: ignore[call-arg]
    """One audited Summer League raw JSON snapshot file.

    A source document in backbone terms (journey-graph §10).
    """

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


class SummerLeagueEdition(SQLModel, table=True):  # type: ignore[call-arg]
    """One normalized Summer League competition for a year and NBA.com LeagueID.

    The recurring series is the competition; this row is one dated instance of
    it — an edition (journey-graph §7).
    """

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
        Index("ix_summer_league_team_entries_team_program_id", "team_program_id"),
        Index(
            "ix_summer_league_team_entries_competition_team_slug",
            "competition_id",
            "team_slug",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    competition_id: int = Field(foreign_key="summer_league_competitions.id")
    nba_team_id: Optional[int] = Field(default=None, foreign_key="nba_teams.id")
    # Generic org-model target, additive alongside nba_team_id (journey-graph
    # §7a, §13; phase-4 spec §5.1 decision D3). No row is ever repointed or
    # nulled -- reads resolve through
    # app.services.player_affiliation.resolve_team_target, which prefers this
    # column and falls back to nba_team_id.
    #
    # Soft reference (no DB-level FK): this table is created by the earlier
    # b6c7d8e9f0a1 create_all() migration, which reflects the live model, so a
    # hard FK here would forward-reference team_programs (created four
    # migrations later in b8c9d0e1f2a3) and break upgrade-from-base. Same
    # trade-off, same reason as participation_id on
    # summer_league_player_game_logs (2f09df4af11c).
    team_program_id: Optional[int] = Field(default=None)
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
    # Scheduled/actual tip-off in UTC (Desk state machine + Morning Card timing).
    # Nullable: legacy rows predate scoreboard ingest (behavior spec §10) and the
    # scoreboard/schedule step that populates this is ticket #515, not this migration.
    tip_datetime: Optional[datetime] = Field(default=None)
    home_team_entry_id: Optional[int] = Field(
        default=None,
        foreign_key="summer_league_team_entries.id",
    )
    away_team_entry_id: Optional[int] = Field(
        default=None,
        foreign_key="summer_league_team_entries.id",
    )
    # Raw NBA Stats provider team IDs (schedule feed's homeTeam.teamId /
    # awayTeam.teamId, stringified), retained independently of the resolved
    # *_team_entry_id FKs above so scoreboard ingest (#529) can report a
    # provider team ID it couldn't resolve to an existing
    # `summer_league_team_entries` row without fabricating a parallel row.
    home_nba_stats_team_id: Optional[str] = Field(default=None)
    away_nba_stats_team_id: Optional[str] = Field(default=None)
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
    # Honest raw provider status text (e.g. "Final/OT", "PPD", "Qtr 3 - 4:12"),
    # kept alongside the coarse `status` enum bucket since the enum alone
    # cannot distinguish an OT final from a regulation final, or a postponed
    # game from a merely-scheduled one.
    status_text: Optional[str] = Field(default=None)
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
    # Tournament round from the NBA.com schedule feed (gameSubLabel), e.g.
    # "Semifinals" / "Championship" / "Consolation". Null for pool-play games
    # and for exhibition venues (Salt Lake / California Classic / Orlando).
    round_label: Optional[str] = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
    updated_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)


class SummerLeagueSourceRecord(SQLModel, table=True):  # type: ignore[call-arg]
    """One NBA.com source player identity before and after canonical resolution.

    A source record in backbone terms (journey-graph §10).
    """

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
        Index(
            "ix_summer_league_player_game_logs_participation_id",
            "participation_id",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    competition_id: int = Field(foreign_key="summer_league_competitions.id")
    game_id: int = Field(foreign_key="summer_league_games.id")
    team_entry_id: int = Field(foreign_key="summer_league_team_entries.id")
    source_player_id: int = Field(foreign_key="summer_league_source_players.id")
    player_id: Optional[int] = Field(default=None, foreign_key="players_master.id")
    # Soft reference to the stable participation bridge; backfilled for 2026+ rows.
    # Deliberately NOT a DB-level FK: summer_league_player_game_logs is created by an
    # earlier create_all() migration (b6c7d8e9f0a1) that reflects the live model, so a
    # hard FK here would forward-reference summer_league_participation (created later in
    # 2f09df4af11c) and break `alembic upgrade head` from base. The link is maintained by
    # the loader/resolution layer; see docs/plans/summer-league-2026-workstream0-schema.md.
    participation_id: Optional[int] = Field(default=None)
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


class SummerLeagueParticipation(SQLModel, table=True):  # type: ignore[call-arg]
    """Stable bridge: one row per (player, team_entry, stint) in a competition.

    Player game logs reference this row, not raw (player, edition). A stint
    captures a mid-competition team change or guest/replacement appearance
    (journey-graph §7b).
    """

    __tablename__ = "summer_league_participation"
    __table_args__ = (
        UniqueConstraint(
            "competition_id",
            "team_entry_id",
            "source_player_id",
            "stint_no",
            name="uq_summer_league_participation_comp_team_source_stint",
        ),
        Index("ix_summer_league_participation_player_id", "player_id"),
        Index(
            "ix_summer_league_participation_competition_team",
            "competition_id",
            "team_entry_id",
        ),
        Index(
            "ix_summer_league_participation_source_player_id",
            "source_player_id",
        ),
        Index("ix_summer_league_participation_affiliation_id", "affiliation_id"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    competition_id: int = Field(foreign_key="summer_league_competitions.id")
    team_entry_id: int = Field(foreign_key="summer_league_team_entries.id")
    source_player_id: int = Field(foreign_key="summer_league_source_players.id")
    # Backfilled on resolution, mirroring the game-log player_id pattern.
    player_id: Optional[int] = Field(default=None, foreign_key="players_master.id")
    # Current roster assertion for this participation (the append-only stream).
    affiliation_id: Optional[int] = Field(
        default=None, foreign_key="player_affiliations.id"
    )
    stint_no: int = Field(default=1, nullable=False)

    # Denormalized current roster state (the assertion history lives in
    # player_affiliations; this is the fast read).
    roster_status: AffiliationStatus = Field(
        default=AffiliationStatus.ANNOUNCED,
        sa_column=Column(
            SAEnum(
                AffiliationStatus, name="affiliation_status_enum", create_type=False
            ),
            nullable=False,
            server_default=AffiliationStatus.ANNOUNCED.value,
        ),
    )
    jersey_number: Optional[str] = Field(default=None)
    roster_position: Optional[str] = Field(default=None)
    first_game_date: Optional[date] = Field(default=None)
    last_game_date: Optional[date] = Field(default=None)
    games_played: Optional[int] = Field(default=None)

    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
    updated_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)


class SummerLeaguePlayerResolutionReview(SQLModel, table=True):  # type: ignore[call-arg]
    """Pending or completed review for ambiguous Summer League player resolution."""

    __tablename__ = "summer_league_player_resolution_reviews"
    __table_args__ = (
        Index(
            "uq_summer_league_player_resolution_reviews_pending_source",
            "source_player_id",
            unique=True,
            postgresql_where=text("status = 'PENDING'"),
        ),
        Index("ix_summer_league_player_resolution_reviews_status", "status"),
        Index(
            "ix_summer_league_player_resolution_reviews_selected_player_id",
            "selected_player_id",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    source_player_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("summer_league_source_players.id"),
            nullable=False,
        )
    )
    raw_player_name: str = Field(nullable=False)
    nba_stats_person_id: str = Field(nullable=False)
    candidate_players: Optional[list[dict[str, object]]] = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
    )
    status: SummerLeagueReviewStatus = Field(
        default=SummerLeagueReviewStatus.PENDING,
        sa_column=Column(
            SAEnum(
                SummerLeagueReviewStatus,
                name="summer_league_review_status_enum",
            ),
            nullable=False,
            server_default=SummerLeagueReviewStatus.PENDING.value,
        ),
    )
    selected_player_id: Optional[int] = Field(
        default=None,
        foreign_key="players_master.id",
    )
    review_note: Optional[str] = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
    reviewed_at: Optional[datetime] = Field(default=None)
