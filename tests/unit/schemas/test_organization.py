"""Unit checks for the organization / team_program / organization_relationship schema.

Verifies:
- OrgKind and OrgRelationshipType are exactly the closed §13.3 sets (fails if a value
  is added without a decision).
- Each table exposes the expected name, named constraints/indexes, and enum type.
- organization_relationships carries the ticket-#798 shape: no blanket
  (from, to, type) unique constraint, a partial unique index scoped to the single
  *open* current edge, and PlayerAffiliation's four bitemporal stamps.
- Model construction populates default timestamps without a DB round-trip.
"""

from __future__ import annotations

from sqlalchemy import UniqueConstraint

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
    assert {index.name for index in table.indexes} >= {
        "ix_organization_relationships_from_organization_id",
        "ix_organization_relationships_to_organization_id",
        "ix_organization_relationships_relationship_type",
        "ix_organization_relationships_supersedes_id",
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


def test_organization_relationship_has_no_blanket_unique_constraint() -> None:
    """The (from, to, type)-forever constraint is gone (ticket #798, design B).

    Its presence is what made a resumed relationship unrecordable, so this
    asserts the *absence* directly rather than trusting the replacement index to
    imply it.
    """
    table = OrganizationRelationship.__table__  # type: ignore[attr-defined]

    constraint_names = {
        constraint.name
        for constraint in table.constraints
        if constraint.name is not None
    }
    assert "uq_organization_relationships_from_to_type" not in constraint_names
    # No unique *constraint* of any name covers the bare triple either.
    for constraint in table.constraints:
        if isinstance(constraint, UniqueConstraint):
            assert {column.name for column in constraint.columns} != {
                "from_organization_id",
                "to_organization_id",
                "relationship_type",
            }


def test_organization_relationship_current_partial_unique_index() -> None:
    """Uniqueness is scoped to the single open, non-superseded, non-retracted edge.

    The ``effective_end IS NULL`` term in the predicate is the load-bearing part:
    without it a closed historical interval would collide with a live one, which
    is the exact defect ticket #798 fixes. Asserted on the rendered predicate so
    a silent narrowing of the WHERE clause fails here.
    """
    table = OrganizationRelationship.__table__  # type: ignore[attr-defined]

    index = next(
        candidate
        for candidate in table.indexes
        if candidate.name == "uq_organization_relationships_current"
    )
    assert index.unique is True
    assert [column.name for column in index.columns] == [
        "from_organization_id",
        "to_organization_id",
        "relationship_type",
    ]
    predicate = str(index.dialect_options["postgresql"]["where"])
    assert "superseded_at IS NULL" in predicate
    assert "retracted_at IS NULL" in predicate
    assert "effective_end IS NULL" in predicate


def test_organization_relationship_bitemporal_columns() -> None:
    """The four PlayerAffiliation-mirroring stamps exist with the right nullability."""
    table = OrganizationRelationship.__table__  # type: ignore[attr-defined]

    assert table.c.recorded_at.nullable is False
    for column_name in ("supersedes_id", "superseded_at", "retracted_at"):
        assert table.c[column_name].nullable is True

    # supersedes_id points back at this same table -- the correction trail.
    supersedes_targets = {
        fk.column.table.name for fk in table.c.supersedes_id.foreign_keys
    }
    assert supersedes_targets == {"organization_relationships"}


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
    assert relationship.supersedes_id is None
    assert relationship.superseded_at is None
    assert relationship.retracted_at is None
    assert relationship.recorded_at is not None
    assert relationship.created_at is not None
    assert relationship.updated_at is not None
