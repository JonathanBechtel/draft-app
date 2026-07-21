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

from alembic import op  # type: ignore[attr-defined]

# revision identifiers, used by Alembic.
revision: str = "e6f7a8b9c0d1"
down_revision: Union[str, None] = "16a524075c8e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add identity/field-composition disclosure columns.

    ``ADD COLUMN IF NOT EXISTS`` guards throughout: the parent tables are
    created by an earlier ``SQLModel.metadata.create_all`` migration that
    reflects the live model classes, so a from-scratch bootstrap already has
    these columns by the time this migration runs (see `16a524075c8e` for the
    same guard rationale).
    """
    op.execute(
        "ALTER TABLE summer_league_environment_profiles "
        "ADD COLUMN IF NOT EXISTS starts_on DATE"
    )
    op.execute(
        "ALTER TABLE summer_league_environment_profiles "
        "ADD COLUMN IF NOT EXISTS ends_on DATE"
    )
    op.execute(
        "ALTER TABLE summer_league_environment_profiles "
        "ADD COLUMN IF NOT EXISTS not_yet_drafted_count INTEGER"
    )
    op.execute(
        "UPDATE summer_league_environment_profiles "
        "SET not_yet_drafted_count = 0 WHERE not_yet_drafted_count IS NULL"
    )
    op.execute(
        "ALTER TABLE summer_league_environment_profiles "
        "ALTER COLUMN not_yet_drafted_count SET NOT NULL, "
        "ALTER COLUMN not_yet_drafted_count SET DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE summer_league_environment_profiles "
        "ADD COLUMN IF NOT EXISTS repeat_participants INTEGER"
    )
    op.execute(
        "ALTER TABLE summer_league_environment_field_composition "
        "ADD COLUMN IF NOT EXISTS reason VARCHAR"
    )


def downgrade() -> None:
    """Drop the added columns."""
    op.execute(
        "ALTER TABLE summer_league_environment_field_composition "
        "DROP COLUMN IF EXISTS reason"
    )
    op.execute(
        "ALTER TABLE summer_league_environment_profiles "
        "DROP COLUMN IF EXISTS repeat_participants"
    )
    op.execute(
        "ALTER TABLE summer_league_environment_profiles "
        "DROP COLUMN IF EXISTS not_yet_drafted_count"
    )
    op.execute(
        "ALTER TABLE summer_league_environment_profiles "
        "DROP COLUMN IF EXISTS ends_on"
    )
    op.execute(
        "ALTER TABLE summer_league_environment_profiles "
        "DROP COLUMN IF EXISTS starts_on"
    )
