"""Add team scoring spread (points IQR) to Competition Context profiles.

Revision ID: 9fae26346abb
Revises: e6f7a8b9c0d1
Create Date: 2026-07-20

Ticket #639 (Competition Context Explorer hardening): the performance-landscape
section published a team offensive-rating spread (``team_ortg_iqr``) but no
distinct scoring-distribution companion, so the detail surface under-delivered
on the frozen release outline's "team offensive-rating spread and scoring
distribution" pair. ``team_points_iqr`` is the same interquartile-spread
treatment applied to raw team points per team-game rather than offensive
rating -- a high-pace/low-ORtg environment can still have a tight scoring
spread, and vice versa, so this is additional signal, not a duplicate of the
existing metric.

This is an additive column on an existing table, so this migration uses a
targeted ``op.add_column``/``op.drop_column`` rather than
``SQLModel.metadata.create_all``/``drop_all`` per the repo migration
convention for existing-table changes. No backfill is required: existing
profile rows read the new column as ``NULL`` (an honest "not yet computed"
state) until the next environment-profile rebuild populates it.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]

# revision identifiers, used by Alembic.
revision: str = "9fae26346abb"
down_revision: Union[str, None] = "e6f7a8b9c0d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the nullable ``team_points_iqr`` column."""
    op.add_column(
        "summer_league_environment_profiles",
        sa.Column("team_points_iqr", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    """Drop the ``team_points_iqr`` column."""
    op.drop_column("summer_league_environment_profiles", "team_points_iqr")
