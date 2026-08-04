"""Add shot-diet rate columns to summer_league_player_seasons.

Revision ID: a1d2e3f4b5c6
Revises: 7a7804ad90cb
Create Date: 2026-06-27

Adds rim_rate, mid_rate, three_rate, corner3_rate (nullable DOUBLE PRECISION)
to the materialized summer_league_player_seasons table.  These are derived from
SummerLeagueShotEvent zone data during metrics.rebuild() and are NULL when shot-
chart data is absent for a player-competition.

Uses ADD COLUMN IF NOT EXISTS so this migration is idempotent on a fresh database
(where SQLModel.metadata.create_all already reflected the new columns from the
prior metrics-table creation migration).
"""

from typing import Sequence, Union

from alembic import op  # type: ignore[attr-defined]

# revision identifiers, used by Alembic.
revision: str = "a1d2e3f4b5c6"
down_revision: Union[str, None] = "7a7804ad90cb"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "summer_league_player_seasons"
_COLS = ("rim_rate", "mid_rate", "three_rate", "corner3_rate")


def upgrade() -> None:
    """Add nullable shot-diet rate columns.

    Guarded with IF NOT EXISTS so a fresh-DB create_all (which already
    reflects the updated SummerLeagueDerivedAgg model) does not error.
    """
    for col in _COLS:
        op.execute(
            f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {col} DOUBLE PRECISION"
        )


def downgrade() -> None:
    """Drop the shot-diet rate columns."""
    for col in reversed(_COLS):
        op.execute(
            f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {col}"
        )
