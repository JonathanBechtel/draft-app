"""Organization / team-program / organization-relationship model.

Tables-only foundation for the second journey-graph spoke (non-NBA affiliations).
Per journey-graph §7a, the hierarchy is deliberately three-deep and must not be
compressed:

    organization            FC Barcelona · a national federation   (corporate / governing)
      -> team_program        FC Barcelona Bàsquet · its U18 squad · U17 national team
            -> team_entry     that team's entry in a specific competition edition

**Player affiliations point at a `TeamProgram`, normally NOT at an `Organization`
directly.** A national team is a `TeamProgram` OWNED by a federation `Organization`
via `OrganizationRelationship` -- the federation itself is never the affiliation
target. See docs/plans/global-player-journey-graph.md §7a, §13.3.

This module ships the three tables only. Nothing here is populated or wired to
existing tables (`player_affiliations`, `summer_league_team_entries`) yet -- see
successor tickets for population and retargeting.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Optional

from sqlalchemy import (
    Column,
    Enum as SAEnum,
    Index,
    UniqueConstraint,
)
from sqlmodel import Field, SQLModel


class OrgKind(str, Enum):
    """Closed discriminator for corporate/governing organizations (journey-graph §13.3)."""

    CLUB = "CLUB"
    FEDERATION = "FEDERATION"
    LEAGUE = "LEAGUE"
    SCHOOL = "SCHOOL"
    ACADEMY = "ACADEMY"
    NATIONAL_PROGRAM = "NATIONAL_PROGRAM"


class OrgRelationshipType(str, Enum):
    """Closed set of typed edges between organizations (journey-graph §7a, §13.3)."""

    OWNS = "OWNS"
    ACADEMY_OF = "ACADEMY_OF"
    FEEDS = "FEEDS"
    AFFILIATED_WITH = "AFFILIATED_WITH"


class Organization(SQLModel, table=True):  # type: ignore[call-arg]
    """A corporate or governing body: a club, federation, league, school, or academy.

    Per journey-graph §7a, player affiliations point at a `TeamProgram`, normally NOT
    at an `Organization` directly -- an `Organization` is the owner/governor of one or
    more `TeamProgram` rows, never the thing a player is rostered to.
    """

    __tablename__ = "organizations"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_organizations_slug"),
        Index("ix_organizations_org_kind", "org_kind"),
        Index("ix_organizations_country", "country"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    org_kind: OrgKind = Field(
        sa_column=Column(
            # Distinct PG type name -- checked against existing enum types in the
            # DB before naming (no collision with org_kind_enum found in the repo).
            SAEnum(OrgKind, name="org_kind_enum"),
            nullable=False,
        )
    )
    name: str = Field(nullable=False)
    slug: str = Field(nullable=False)
    country: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
    updated_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)


class TeamProgram(SQLModel, table=True):  # type: ignore[call-arg]
    """The competitive team/squad that player affiliations point at (journey-graph §7a).

    Distinct from its parent `Organization` (the corporate/governing body that owns
    or governs it) and from a `team_entry` (that team's entry in one competition
    edition -- SL-namespaced today as `summer_league_team_entries`). A national team
    is a `TeamProgram` OWNED by a federation `Organization`, not the federation
    itself; see `OrganizationRelationship`.
    """

    __tablename__ = "team_programs"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_team_programs_slug"),
        Index("ix_team_programs_organization_id", "organization_id"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    organization_id: int = Field(foreign_key="organizations.id", nullable=False)
    name: str = Field(nullable=False)
    slug: str = Field(nullable=False)
    level: Optional[str] = Field(
        default=None, description="e.g. 'U18', 'senior', 'varsity'"
    )
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
    updated_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)


class OrganizationRelationship(SQLModel, table=True):  # type: ignore[call-arg]
    """A typed edge between two organizations (journey-graph §7a).

    E.g. a national federation OWNS the org fielding its national team program, or a
    feeder club is ACADEMY_OF a parent club. Per decision D2
    (docs/plans/summer-league-phase4-journey-graph-conversion-spec.md §5.1), rows
    ship with this table only where a real relationship is known -- this ticket
    creates the table, it does not populate it.
    """

    __tablename__ = "organization_relationships"
    __table_args__ = (
        UniqueConstraint(
            "from_organization_id",
            "to_organization_id",
            "relationship_type",
            name="uq_organization_relationships_from_to_type",
        ),
        Index(
            "ix_organization_relationships_from_organization_id",
            "from_organization_id",
        ),
        Index(
            "ix_organization_relationships_to_organization_id",
            "to_organization_id",
        ),
        Index(
            "ix_organization_relationships_relationship_type",
            "relationship_type",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    from_organization_id: int = Field(foreign_key="organizations.id", nullable=False)
    to_organization_id: int = Field(foreign_key="organizations.id", nullable=False)
    relationship_type: OrgRelationshipType = Field(
        sa_column=Column(
            # Distinct PG type name -- checked against existing enum types in the
            # DB before naming (no collision with organization_relationship_type_enum
            # found in the repo).
            SAEnum(OrgRelationshipType, name="organization_relationship_type_enum"),
            nullable=False,
        )
    )
    effective_start: Optional[date] = Field(default=None)
    effective_end: Optional[date] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
    updated_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
