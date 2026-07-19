"""Add Summer League Competition Context environment profile tables.

Revision ID: a7b8c9d0e1f2
Revises: g7h8i9j0k1l2
Create Date: 2026-07-19

New derived, versioned read-model tables for the Competition Context Explorer
(issue #606): the profile with typed metric columns and its season-membership,
per-metric coverage, field-composition, and provenance child tables. All are
brand-new tables → created wholesale via ``SQLModel.metadata.create_all`` per
the repo migration convention (no autogenerate DDL for existing tables). See
``docs/plans/competition-context-explorer-first-release.md``.

Downgrade note: these tables define no PostgreSQL enum types, so downgrade drops
them with explicit ``op.drop_table`` calls (children before the parent) rather
than ``SQLModel.metadata.drop_all``. ``drop_all`` against the *shared* metadata
also emits ``DROP TYPE`` for every metadata-bound enum (e.g.
``board_status_enum``) still in use by other tables, which raises
``DependentObjectsStillExistError`` — a pre-existing repo-wide quirk that the
targeted drops here sidestep for a clean, tested round-trip.
"""

from typing import Sequence, Union

from alembic import op  # type: ignore[attr-defined]
from sqlmodel import SQLModel

from app.schemas.summer_league_environment import (
    SummerLeagueEnvironmentFieldComposition,
    SummerLeagueEnvironmentMetricCoverage,
    SummerLeagueEnvironmentProfile,
    SummerLeagueEnvironmentProvenance,
    SummerLeagueEnvironmentSeasonMembership,
)

# revision identifiers, used by Alembic.
revision: str = "a7b8c9d0e1f2"
down_revision: Union[str, None] = "g7h8i9j0k1l2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Parent profile first, children after (FK order); reverse on downgrade.
_TABLES = [
    SummerLeagueEnvironmentProfile.__table__,  # type: ignore[attr-defined]
    SummerLeagueEnvironmentSeasonMembership.__table__,  # type: ignore[attr-defined]
    SummerLeagueEnvironmentMetricCoverage.__table__,  # type: ignore[attr-defined]
    SummerLeagueEnvironmentFieldComposition.__table__,  # type: ignore[attr-defined]
    SummerLeagueEnvironmentProvenance.__table__,  # type: ignore[attr-defined]
]


def upgrade() -> None:
    SQLModel.metadata.create_all(bind=op.get_bind(), tables=_TABLES)


def downgrade() -> None:
    # Drop children before the parent profile (FK order). Explicit drops avoid
    # metadata.drop_all leaking DROP TYPE for shared metadata-bound enums.
    op.drop_table("summer_league_environment_provenance")
    op.drop_table("summer_league_environment_field_composition")
    op.drop_table("summer_league_environment_metric_coverage")
    op.drop_table("summer_league_environment_season_memberships")
    op.drop_table("summer_league_environment_profiles")
