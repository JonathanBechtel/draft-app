"""Unit checks for the organization / team_program / organization_relationship schema.

Verifies:
- OrgKind and OrgRelationshipType are exactly the closed §13.3 sets (fails if a value
  is added without a decision).
- Each table exposes the expected name, named constraints/indexes, and enum type.
- Model construction populates default timestamps without a DB round-trip.
"""

from __future__ import annotations

from app.schemas.organization import (
    OrgKind,
    OrganizationRelationship,
    OrgRelationshipType,
    Organization,
    TeamProgram,
)


def test_org_kind_is_the_closed_set() -> None:
    """OrgKind exactly matches the closed §13.3 set."""
    assert [kind.value for kind in OrgKind] == [
        "CLUB",
        "FEDERATION",
        "LEAGUE",
        "SCHOOL",
        "ACADEMY",
        "NATIONAL_PROGRAM",
    ]


def test_org_relationship_type_is_the_closed_set() -> None:
    """OrgRelationshipType exactly matches the closed §13.3 set."""
    assert [rel.value for rel in OrgRelationshipType] == [
        "OWNS",
        "ACADEMY_OF",
        "FEEDS",
        "AFFILIATED_WITH",
    ]


def test_organization_table_contract() -> None:
    """organizations table has the expected name, unique slug, and named indexes."""
    table = Organization.__table__  # type: ignore[attr-defined]

    assert table.name == "organizations"
    assert {
        constraint.name
        for constraint in table.constraints
        if constraint.name is not None
    } >= {"uq_organizations_slug"}
    assert {index.name for index in table.indexes} >= {
        "ix_organizations_org_kind",
        "ix_organizations_country",
    }
    assert table.c.org_kind.type.name == "org_kind_enum"


def test_team_program_table_contract() -> None:
    """team_programs table has the expected name, unique slug, FK, and index."""
    table = TeamProgram.__table__  # type: ignore[attr-defined]

    assert table.name == "team_programs"
    assert {
        constraint.name
        for constraint in table.constraints
        if constraint.name is not None
    } >= {"uq_team_programs_slug"}
    assert {index.name for index in table.indexes} >= {
        "ix_team_programs_organization_id"
    }
    fk_targets = {fk.column.table.name for fk in table.c.organization_id.foreign_keys}
    assert fk_targets == {"organizations"}


def test_organization_relationship_table_contract() -> None:
    """organization_relationships table has the expected name, constraints, and indexes."""
    table = OrganizationRelationship.__table__  # type: ignore[attr-defined]

    assert table.name == "organization_relationships"
    assert {
        constraint.name
        for constraint in table.constraints
        if constraint.name is not None
    } >= {"uq_organization_relationships_from_to_type"}
    assert {index.name for index in table.indexes} >= {
        "ix_organization_relationships_from_organization_id",
        "ix_organization_relationships_to_organization_id",
        "ix_organization_relationships_relationship_type",
    }
    assert (
        table.c.relationship_type.type.name == "organization_relationship_type_enum"
    )
    from_fk_targets = {
        fk.column.table.name for fk in table.c.from_organization_id.foreign_keys
    }
    to_fk_targets = {
        fk.column.table.name for fk in table.c.to_organization_id.foreign_keys
    }
    assert from_fk_targets == {"organizations"}
    assert to_fk_targets == {"organizations"}


def test_model_construction_defaults() -> None:
    """Constructing each model without timestamps relies on default_factory, not None."""
    org = Organization(
        org_kind=OrgKind.FEDERATION, name="Test Federation", slug="test-federation"
    )
    assert org.id is None
    assert org.country is None
    assert org.created_at is not None
    assert org.updated_at is not None

    program = TeamProgram(organization_id=1, name="Test U18", slug="test-u18")
    assert program.id is None
    assert program.level is None
    assert program.created_at is not None
    assert program.updated_at is not None

    relationship = OrganizationRelationship(
        from_organization_id=1,
        to_organization_id=2,
        relationship_type=OrgRelationshipType.OWNS,
    )
    assert relationship.id is None
    assert relationship.effective_start is None
    assert relationship.effective_end is None
    assert relationship.created_at is not None
    assert relationship.updated_at is not None
