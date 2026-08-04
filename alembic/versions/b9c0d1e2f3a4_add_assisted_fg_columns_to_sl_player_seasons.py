"""Add assisted-FG count columns to summer_league_player_seasons.

Revision ID: b9c0d1e2f3a4
Revises: a1d2e3f4b5c6
Create Date: 2026-06-27

Adds ast_fgm and unast_fgm (nullable INTEGER) to the materialized
summer_league_player_seasons table.  These are derived from
SummerLeaguePlayByPlayEvent made-FG events (event_msg_type=1) during
metrics.rebuild() and are NULL when no PBP made-FG data exists for a
player-competition.

Uses ADD COLUMN IF NOT EXISTS so this migration is idempotent on a fresh
database (where SQLModel.metadata.create_all already reflects the updated
SummerLeagueDerivedAgg model from a prior create_all call).
"""

from typing import Sequence, Union

from alembic import op  # type: ignore[attr-defined]

# revision identifiers, used by Alembic.
revision: str = "b9c0d1e2f3a4"
down_revision: Union[str, None] = "a1d2e3f4b5c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "summer_league_player_seasons"
_COLS = ("ast_fgm", "unast_fgm")


def upgrade() -> None:
    """Add nullable assisted-FG count columns.

    Guarded with IF NOT EXISTS so a fresh-DB create_all (which already
    reflects the updated SummerLeagueDerivedAgg model) does not error.
    """
    for col in _COLS:
        op.execute(
            f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {col} INTEGER"
        )


def downgrade() -> None:
    """Drop the assisted-FG count columns."""
    for col in reversed(_COLS):
        op.execute(
            f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {col}"
        )
