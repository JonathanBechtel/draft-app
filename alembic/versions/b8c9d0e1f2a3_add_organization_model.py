"""Add organization / team_program / organization_relationship model.

Revision ID: b8c9d0e1f2a3
Revises: a0b1c2d3e4f6
Create Date: 2026-08-03

Tables-only foundation for the second journey-graph spoke (ticket #781):
organizations, team_programs, organization_relationships. All three are
brand-new tables -> created wholesale via ``SQLModel.metadata.create_all``
per the repo migration convention (no autogenerate DDL for existing tables).
Nothing else references these tables yet; nothing is populated by this
migration. See docs/plans/global-player-journey-graph.md §7a, §13.3 and
docs/plans/summer-league-phase4-journey-graph-conversion-spec.md §5.1 (D2).

Two new PG enum types:
  - org_kind_enum (organizations.org_kind).
  - organization_relationship_type_enum (organization_relationships.relationship_type).

Downgrade note: these tables define enum types used nowhere else, so downgrade
drops the tables with explicit ``op.drop_table`` (children before the parent,
mirroring the create_all FK order) and then drops the two enum types
explicitly -- the same pattern used in 2f09df4af11c -- rather than
``SQLModel.metadata.drop_all``, whose blanket ``DROP TYPE`` would also target
enum types shared with unrelated tables still in the database.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op  # type: ignore[attr-defined]
from sqlalchemy.dialects import postgresql
from sqlmodel import SQLModel

from app.schemas.organization import (
    Organization,
    OrganizationRelationship,
    TeamProgram,
)

# revision identifiers, used by Alembic.
revision: str = "b8c9d0e1f2a3"
down_revision: Union[str, None] = "a0b1c2d3e4f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Parent organizations first, then its dependents (FK order); reverse on downgrade.
_TABLES = [
    Organization.__table__,  # type: ignore[attr-defined]
    TeamProgram.__table__,  # type: ignore[attr-defined]
    OrganizationRelationship.__table__,  # type: ignore[attr-defined]
]


def upgrade() -> None:
    """Create organizations, team_programs, and organization_relationships."""
    SQLModel.metadata.create_all(bind=op.get_bind(), tables=_TABLES)


def downgrade() -> None:
    """Drop all three tables and their enum types."""
    bind = op.get_bind()

    # Drop children before the parent (FK order).
    op.drop_table("organization_relationships")
    op.drop_table("team_programs")
    op.drop_table("organizations")

    # Drop the enum types created implicitly by create_all above.
    postgresql.ENUM(name="organization_relationship_type_enum").drop(
        bind, checkfirst=True
    )
    postgresql.ENUM(name="org_kind_enum").drop(bind, checkfirst=True)
