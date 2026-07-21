"""Add Competition Context identity/field-composition fields.

Revision ID: e6f7a8b9c0d1
Revises: 16a524075c8e
Create Date: 2026-07-20

Ticket #638 (Competition Context Explorer hardening): the first-release
identity and field-composition sections were incomplete -- no competition
start/end dates, no distinct not-yet-drafted count, no repeat-participant
disclosure, and no per-attribute fallback-usage/unavailability reason. These
are additive columns on existing tables, so this migration uses targeted
``op.add_column``/``op.drop_column`` rather than
``SQLModel.metadata.create_all``/``drop_all`` per the repo migration
convention for existing-table changes.

* ``summer_league_environment_profiles.starts_on`` / ``.ends_on`` -- the
  scope's competition start/end dates (min/max across members for a season
  scope).
* ``summer_league_environment_profiles.not_yet_drafted_count`` -- distinct
  from ``undrafted_count`` (contract §5: "not yet drafted", never
  retrospectively undrafted). Backfilled to 0 for already-published rows.
* ``summer_league_environment_profiles.repeat_participants`` -- season scope
  only; canonical players appearing in more than one member competition.
* ``summer_league_environment_field_composition.reason`` -- an optional
  honesty caveat (fallback-usage disclosure, or an explicit "not yet
  supported" note for origin).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]

# revision identifiers, used by Alembic.
revision: str = "e6f7a8b9c0d1"
down_revision: Union[str, None] = "16a524075c8e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add identity/field-composition disclosure columns."""
    op.add_column(
        "summer_league_environment_profiles",
        sa.Column("starts_on", sa.Date(), nullable=True),
    )
    op.add_column(
        "summer_league_environment_profiles",
        sa.Column("ends_on", sa.Date(), nullable=True),
    )
    op.add_column(
        "summer_league_environment_profiles",
        sa.Column("not_yet_drafted_count", sa.Integer(), nullable=True),
    )
    op.execute(
        "UPDATE summer_league_environment_profiles "
        "SET not_yet_drafted_count = 0 WHERE not_yet_drafted_count IS NULL"
    )
    op.alter_column(
        "summer_league_environment_profiles",
        "not_yet_drafted_count",
        nullable=False,
        server_default="0",
    )
    op.add_column(
        "summer_league_environment_profiles",
        sa.Column("repeat_participants", sa.Integer(), nullable=True),
    )
    op.add_column(
        "summer_league_environment_field_composition",
        sa.Column("reason", sa.String(), nullable=True),
    )


def downgrade() -> None:
    """Drop the added columns."""
    op.drop_column("summer_league_environment_field_composition", "reason")
    op.drop_column("summer_league_environment_profiles", "repeat_participants")
    op.drop_column("summer_league_environment_profiles", "not_yet_drafted_count")
    op.drop_column("summer_league_environment_profiles", "ends_on")
    op.drop_column("summer_league_environment_profiles", "starts_on")
