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
    text,
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
    """An append-only, bitemporal typed edge between two organizations (journey-graph §7a).

    E.g. a national federation OWNS the org fielding its national team program, or a
    feeder club is ACADEMY_OF a parent club. Per decision D2
    (docs/plans/summer-league-phase4-journey-graph-conversion-spec.md §5.1), rows land
    only where a real relationship is known.

    Design rationale (ticket #798) -- please do not re-litigate
    ----------------------------------------------------------
    As originally shipped (#781) this table carried
    ``UniqueConstraint(from_organization_id, to_organization_id, relationship_type)``:
    one row per triple, *forever*. Combined with ``effective_start`` / ``effective_end``
    that was self-contradictory -- the columns model a time-bounded edge, but the
    constraint forbids the second interval. A club that was ``ACADEMY_OF`` a parent,
    separated, and later re-affiliated could only be recorded by mutating the first
    interval away or by widening the span into a claim that is false for the years in
    between. Both destroy history, contradicting P2 of
    ``docs/plans/north-star-architecture.md``.

    Two designs were on the table (#798). **Design B was chosen: mirror the sibling
    ``PlayerAffiliation`` (app/schemas/player_affiliation.py).** The blanket unique
    constraint is gone, and the bitemporal stamps (``recorded_at`` / ``supersedes_id``
    / ``superseded_at`` / ``retracted_at``) arrive with the same meanings they carry
    there: ``effective_*`` is when the edge was true in the world, ``recorded_at`` is
    when DraftGuru learned it, and a correction *supersedes* a prior assertion instead
    of mutating it.

    Why B and not A (adding ``effective_start`` to the unique constraint): this is a hub
    table that will receive assertions from multiple independent sources once spoke #2
    lands. An edge asserted by FIBA and later corrected needs a correction trail, which
    a time-scoped unique constraint cannot express -- under A the only way to fix a
    wrong assertion is to overwrite it, losing the record that we ever believed it.
    Design A is also *weaker* on its own terms: it happily admits two rows with
    different starts and open-ended ends, i.e. overlapping intervals for the same edge,
    which is a different silent wrong. Backbone consistency decides the rest -- the
    supersession shape is what this codebase already uses for exactly this problem, and
    "encode principles as types" (north-star build practices) says the correct shape
    should be the default rather than something each reader re-derives.

    One deliberate refinement of the ticket's sketch: the partial unique index is scoped
    ``WHERE superseded_at IS NULL AND retracted_at IS NULL AND effective_end IS NULL``.
    The ``effective_end IS NULL`` term is load-bearing. Without it, a *closed* historical
    interval (ended in 2010) and a live one (resumed in 2015) would collide, and the only
    escape would be to mark the closed interval ``superseded`` -- but supersession means
    "we were wrong", not "it ended", and conflating the two is precisely the history loss
    this ticket exists to remove. With it, the index says the true invariant: **at most
    one open-ended current edge per (from, to, type)**, and any number of closed
    historical intervals. That keeps the duplicate protection the old constraint gave for
    the common undated/open case, while permitting an unlimited resume history.

    Known limit: nothing here forbids two *closed* intervals that overlap. Enforcing that
    needs ``EXCLUDE USING gist`` over a ``daterange``, which requires the ``btree_gist``
    extension; it is deliberately deferred until a source actually produces dated org
    edges (none does today -- see #805). Interval sanity is an ingest-time concern until
    then.
    """

    __tablename__ = "organization_relationships"
    __table_args__ = (
        # NOTE: no blanket UniqueConstraint on (from, to, type) -- see the class
        # docstring. Uniqueness applies only to the *current open* assertion.
        Index(
            "uq_organization_relationships_current",
            "from_organization_id",
            "to_organization_id",
            "relationship_type",
            unique=True,
            postgresql_where=text(
                "superseded_at IS NULL "
                "AND retracted_at IS NULL "
                "AND effective_end IS NULL"
            ),
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
        Index(
            "ix_organization_relationships_supersedes_id",
            "supersedes_id",
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

    # Bitemporal stamps, identical in meaning to PlayerAffiliation's (journey-graph
    # §5b): effective_* = when the edge was true in the world; recorded_at = when
    # DraftGuru learned it; supersedes_id/superseded_at/retracted_at = the correction
    # trail. A resumed relationship is a *new row* with its own effective_start, not a
    # supersession of the closed one.
    effective_start: Optional[date] = Field(default=None)
    effective_end: Optional[date] = Field(default=None)
    recorded_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
    supersedes_id: Optional[int] = Field(
        default=None, foreign_key="organization_relationships.id"
    )
    superseded_at: Optional[datetime] = Field(default=None)
    retracted_at: Optional[datetime] = Field(default=None)

    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
    updated_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
