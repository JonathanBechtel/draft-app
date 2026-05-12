"""Current lifecycle state for a player."""

from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import Column, Enum as SAEnum, UniqueConstraint
from sqlmodel import Field, SQLModel


class PlayerLifecycleStage(str, Enum):
    """Lifecycle stages for a player's current state."""

    RECRUIT = "recruit"
    HIGH_SCHOOL = "high_school"
    COLLEGE = "college"
    INTERNATIONAL_AMATEUR = "international_amateur"
    DRAFT_DECLARED = "draft_declared"
    DRAFT_WITHDREW = "draft_withdrew"
    DRAFTED_NOT_IN_NBA = "drafted_not_in_nba"
    NBA_ACTIVE = "nba_active"
    PRO_NON_NBA = "pro_non_nba"
    INACTIVE_FORMER = "inactive_former"
    UNKNOWN = "unknown"


class CompetitionContext(str, Enum):
    """Competition contexts for a player's current environment."""

    HIGH_SCHOOL = "high_school"
    NCAA = "ncaa"
    INTERNATIONAL = "international"
    NBA = "nba"
    G_LEAGUE = "g_league"
    OVERSEAS_PRO = "overseas_pro"
    INACTIVE = "inactive"
    UNKNOWN = "unknown"


class DraftStatus(str, Enum):
    """Current draft status for a player."""

    NOT_ELIGIBLE = "not_eligible"
    ELIGIBLE = "eligible"
    DECLARED = "declared"
    WITHDREW = "withdrew"
    DRAFTED = "drafted"
    UNDRAFTED = "undrafted"
    UNKNOWN = "unknown"


class CareerStatus(str, Enum):
    """Current career status for a player."""

    ACTIVE = "active"
    FREE_AGENT = "free_agent"
    PROSPECT = "prospect"
    G_LEAGUE = "g_league"
    OVERSEAS = "overseas"
    RETIRED = "retired"
    UNDRAFTED = "undrafted"
    UNKNOWN = "unknown"


class AffiliationType(str, Enum):
    """Type of current player affiliation."""

    HIGH_SCHOOL = "high_school"
    COLLEGE_TEAM = "college_team"
    COMMITTED_SCHOOL = "committed_school"
    NBA_TEAM = "nba_team"
    G_LEAGUE_TEAM = "g_league_team"
    OVERSEAS_CLUB = "overseas_club"
    INDEPENDENT = "independent"
    UNKNOWN = "unknown"


class CommitmentStatus(str, Enum):
    """Status of a player's school commitment."""

    COMMITTED = "committed"
    SIGNED = "signed"
    ENROLLED = "enrolled"
    DECOMMITTED = "decommitted"
    NONE = "none"
    UNKNOWN = "unknown"


class PlayerLifecycle(SQLModel, table=True):  # type: ignore[call-arg]
    """Current lifecycle state and projected draft context for a player."""

    __tablename__ = "player_lifecycle"
    __table_args__ = (UniqueConstraint("player_id", name="uq_player_lifecycle_player"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    player_id: int = Field(foreign_key="players_master.id", index=True)

    lifecycle_stage: PlayerLifecycleStage = Field(
        default=PlayerLifecycleStage.UNKNOWN,
        sa_column=Column(
            SAEnum(
                PlayerLifecycleStage,
                name="player_lifecycle_stage_enum",
            ),
            nullable=False,
            server_default=PlayerLifecycleStage.UNKNOWN.name,
        ),
    )
    competition_context: CompetitionContext = Field(
        default=CompetitionContext.UNKNOWN,
        sa_column=Column(
            SAEnum(
                CompetitionContext,
                name="competition_context_enum",
            ),
            nullable=False,
            server_default=CompetitionContext.UNKNOWN.name,
        ),
    )
    draft_status: DraftStatus = Field(
        default=DraftStatus.UNKNOWN,
        sa_column=Column(
            SAEnum(
                DraftStatus,
                name="draft_status_enum",
            ),
            nullable=False,
            server_default=DraftStatus.UNKNOWN.name,
        ),
    )
    career_status: CareerStatus = Field(
        default=CareerStatus.UNKNOWN,
        sa_column=Column(
            SAEnum(
                CareerStatus,
                name="career_status_enum",
            ),
            nullable=False,
            server_default=CareerStatus.UNKNOWN.name,
        ),
    )

    expected_draft_year: Optional[int] = Field(default=None, index=True)
    current_affiliation_name: Optional[str] = Field(default=None, index=True)
    current_affiliation_type: AffiliationType = Field(
        default=AffiliationType.UNKNOWN,
        sa_column=Column(
            SAEnum(
                AffiliationType,
                name="affiliation_type_enum",
            ),
            nullable=False,
            server_default=AffiliationType.UNKNOWN.name,
        ),
    )
    commitment_school: Optional[str] = Field(default=None, index=True)
    commitment_status: CommitmentStatus = Field(
        default=CommitmentStatus.UNKNOWN,
        sa_column=Column(
            SAEnum(
                CommitmentStatus,
                name="commitment_status_enum",
            ),
            nullable=False,
            server_default=CommitmentStatus.UNKNOWN.name,
        ),
    )
    is_draft_prospect: Optional[bool] = Field(default=None, index=True)

    source: Optional[str] = Field(default=None, description="Provenance key")
    confidence: Optional[float] = Field(default=None)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
