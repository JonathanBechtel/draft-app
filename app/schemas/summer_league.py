"""Summer League raw audit and normalized product schemas."""

from __future__ import annotations

from datetime import datetime
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
