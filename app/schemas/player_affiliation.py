"""Universal player-affiliation assertions (append-only, bitemporal).

An affiliation asserts that a player belonged to a team/program over an interval,
as learned from a source at a recorded time. Corrections supersede prior assertions
rather than mutating them, so historical answers never shift after a backfill.
See docs/plans/global-player-journey-graph.md §5b.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Optional

from sqlalchemy import (
    Column,
    Enum as SAEnum,
    Index,
    Text,
    text,
)
from sqlmodel import Field, SQLModel


class AffiliationType(str, Enum):
    """Scope of an affiliation (relaxes 'exactly one' — see journey-graph §7b)."""

    SUMMER_LEAGUE_ROSTER = "SUMMER_LEAGUE_ROSTER"
    # Reserved for later spokes (additive — no migration of SL rows):
    CLUB = "CLUB"
    NATIONAL_TEAM = "NATIONAL_TEAM"
    NBA_CONTRACT = "NBA_CONTRACT"
    COLLEGE = "COLLEGE"


class AffiliationStatus(str, Enum):
    """Lifecycle of a roster/affiliation assertion."""

    ANNOUNCED = "ANNOUNCED"  # named on a pre-event roster, no game yet
    CONFIRMED = "CONFIRMED"  # corroborated (e.g., appeared in a box score)
    ACTIVE = "ACTIVE"
    CUT = "CUT"  # dropped from a later roster pull
    WITHDRAWN = "WITHDRAWN"


class PlayerAffiliation(SQLModel, table=True):  # type: ignore[call-arg]
    """One append-only affiliation assertion for a canonical player."""

    __tablename__ = "player_affiliations"
    __table_args__ = (
        Index("ix_player_affiliations_player_id", "player_id"),
        Index("ix_player_affiliations_nba_team_id", "nba_team_id"),
        Index("ix_player_affiliations_status", "status"),
        Index("ix_player_affiliations_supersedes_id", "supersedes_id"),
        # Fast "current assertions" lookup — not yet superseded/retracted.
        Index(
            "ix_player_affiliations_active",
            "player_id",
            "nba_team_id",
            postgresql_where=text("superseded_at IS NULL AND retracted_at IS NULL"),
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    # Canonical player; nullable while a roster name is still unresolved
    # (a stub is created when --create-stubs is set, giving a non-null id).
    player_id: Optional[int] = Field(default=None, foreign_key="players_master.id")

    # Affiliation target. For SL the program is the NBA franchise; the generic
    # team/program FK is deferred (journey-graph §7a, §13) and lands additively
    # as a nullable column — SL rows are never repointed.
    nba_team_id: Optional[int] = Field(default=None, foreign_key="nba_teams.id")
    # team_program_id: reserved — added when the generic org model ships.

    affiliation_type: AffiliationType = Field(
        sa_column=Column(
            # Use a distinct PG type name to avoid collision with the existing
            # affiliation_type_enum (player_lifecycle table, different values).
            SAEnum(AffiliationType, name="player_affiliation_type_enum"),
            nullable=False,
        )
    )
    status: AffiliationStatus = Field(
        default=AffiliationStatus.ANNOUNCED,
        sa_column=Column(
            SAEnum(AffiliationStatus, name="affiliation_status_enum"),
            nullable=False,
            server_default=AffiliationStatus.ANNOUNCED.value,
        ),
    )

    # Bitemporal stamps (journey-graph §5b): effective_* = when true in the world;
    # recorded_at = when DraftGuru learned it; superseded_at/retracted_at = correction.
    effective_start: Optional[date] = Field(default=None)
    effective_end: Optional[date] = Field(default=None)
    recorded_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
    supersedes_id: Optional[int] = Field(
        default=None, foreign_key="player_affiliations.id"
    )
    superseded_at: Optional[datetime] = Field(default=None)
    retracted_at: Optional[datetime] = Field(default=None)

    # Provenance — minimal now; Tier-1 assertion_evidence supersedes this pointer.
    source: str = Field(nullable=False)  # e.g. "nba_summer_league_roster"
    source_ref: Optional[str] = Field(default=None, sa_column=Column(Text))

    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
    updated_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
