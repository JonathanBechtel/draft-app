"""Integration tests for the organization / team_program / organization_relationship schema.

Persists rows into a disposable database and asserts:
- An organization, a team program under it, and a typed relationship between two
  organizations can all be created and read back.
- FK enforcement: a team_program cannot reference a nonexistent organization, and an
  organization_relationship cannot reference nonexistent organizations.
- Unique-slug behavior on organizations and team_programs.
- Current-edge uniqueness on organization_relationships: at most one *open*
  (from_organization_id, to_organization_id, relationship_type) assertion. The
  history/resume behavior this replaced a blanket unique constraint with lives in
  test_organization_relationship_intervals.py (ticket #798).

These tests run against the live integration-test Postgres schema (via db_session from
conftest), so they require TEST_DATABASE_URL and PYTEST_ALLOW_DB=1.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.organization import (
    OrgKind,
    Organization,
    OrganizationRelationship,
    OrgRelationshipType,
    TeamProgram,
)


async def _make_organization(
    db: AsyncSession, *, slug: str, org_kind: OrgKind = OrgKind.FEDERATION
) -> Organization:
    """Seed one organization for FK targets."""
    org = Organization(org_kind=org_kind, name=f"Org {slug}", slug=slug)
    db.add(org)
    await db.flush()
    assert org.id is not None
    return org


@pytest.mark.asyncio
async def test_create_organization_team_program_and_relationship(
    db_session: AsyncSession,
) -> None:
    """Create an organization, a team program under it, and a typed relationship."""
    federation = await _make_organization(
        db_session, slug="test-federation", org_kind=OrgKind.FEDERATION
    )
    club = await _make_organization(db_session, slug="test-club", org_kind=OrgKind.CLUB)

    program = TeamProgram(
        organization_id=club.id,
        name="Test Club U18",
        slug="test-club-u18",
        level="U18",
    )
    db_session.add(program)

    relationship = OrganizationRelationship(
        from_organization_id=federation.id,
        to_organization_id=club.id,
        relationship_type=OrgRelationshipType.OWNS,
    )
    db_session.add(relationship)
    await db_session.commit()

    persisted_program = (
        await db_session.execute(
            select(TeamProgram).where(TeamProgram.slug == "test-club-u18")
        )
    ).scalar_one()
    assert persisted_program.organization_id == club.id
    assert persisted_program.level == "U18"

    persisted_relationship = (
        await db_session.execute(
            select(OrganizationRelationship).where(
                OrganizationRelationship.from_organization_id == federation.id,
                OrganizationRelationship.to_organization_id == club.id,
            )
        )
    ).scalar_one()
    assert persisted_relationship.relationship_type == OrgRelationshipType.OWNS


@pytest.mark.asyncio
async def test_organization_slug_uniqueness(db_session: AsyncSession) -> None:
    """Duplicate organization slugs raise IntegrityError."""
    db_session.add(
        Organization(org_kind=OrgKind.CLUB, name="Dup Org A", slug="dup-org")
    )
    await db_session.commit()

    db_session.add(
        Organization(org_kind=OrgKind.CLUB, name="Dup Org B", slug="dup-org")
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_team_program_slug_uniqueness(db_session: AsyncSession) -> None:
    """Duplicate team_program slugs raise IntegrityError."""
    org = await _make_organization(db_session, slug="slug-uniq-org")
    await db_session.commit()

    db_session.add(
        TeamProgram(organization_id=org.id, name="Program A", slug="dup-program")
    )
    await db_session.commit()

    db_session.add(
        TeamProgram(organization_id=org.id, name="Program B", slug="dup-program")
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_team_program_requires_existing_organization(
    db_session: AsyncSession,
) -> None:
    """A team_program cannot reference a nonexistent organization_id."""
    db_session.add(
        TeamProgram(organization_id=999_999, name="Orphan Program", slug="orphan-program")
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_organization_relationship_requires_existing_organizations(
    db_session: AsyncSession,
) -> None:
    """An organization_relationship cannot reference nonexistent organizations."""
    org = await _make_organization(db_session, slug="rel-fk-org")
    await db_session.commit()

    db_session.add(
        OrganizationRelationship(
            from_organization_id=org.id,
            to_organization_id=999_999,
            relationship_type=OrgRelationshipType.FEEDS,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_organization_relationship_uniqueness(db_session: AsyncSession) -> None:
    """(from, to, type) is unique among *open, uncorrected* assertions (#798).

    Both rows below leave effective_end/superseded_at/retracted_at NULL, so they
    both fall inside uq_organization_relationships_current's predicate and the
    duplicate is still rejected -- the protection the original blanket constraint
    provided for the undated case is retained.
    """
    org_a = await _make_organization(db_session, slug="rel-uniq-a")
    org_b = await _make_organization(db_session, slug="rel-uniq-b")
    await db_session.commit()
    # Capture ids before any rollback -- rollback expires ORM attributes, and
    # AsyncSession can't lazily reload an expired attribute outside an explicit
    # await, so re-reading org_a.id/org_b.id post-rollback raises MissingGreenlet.
    org_a_id, org_b_id = org_a.id, org_b.id

    db_session.add(
        OrganizationRelationship(
            from_organization_id=org_a_id,
            to_organization_id=org_b_id,
            relationship_type=OrgRelationshipType.AFFILIATED_WITH,
        )
    )
    await db_session.commit()

    db_session.add(
        OrganizationRelationship(
            from_organization_id=org_a_id,
            to_organization_id=org_b_id,
            relationship_type=OrgRelationshipType.AFFILIATED_WITH,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()

    # A different relationship_type between the same pair is allowed.
    db_session.add(
        OrganizationRelationship(
            from_organization_id=org_a_id,
            to_organization_id=org_b_id,
            relationship_type=OrgRelationshipType.FEEDS,
        )
    )
    await db_session.commit()
