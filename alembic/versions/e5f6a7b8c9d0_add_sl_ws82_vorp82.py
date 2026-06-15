"""Add WS/82 and VORP/82 projection columns to SL player seasons.

Splits the value metrics into a cumulative stat (``ws`` / ``vorp``) and a
full-season projection (``ws82`` / ``vorp82``). ``vorp`` previously held the
projection; the rebuild now writes the cumulative value there and the projection
into ``vorp82``, so a metrics rebuild must follow this migration.

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-06-15

"""

from typing import Sequence, Union

from alembic import op  # type: ignore[attr-defined]

# revision identifiers, used by Alembic.
revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "summer_league_player_seasons"


def upgrade() -> None:
    """Add the nullable ``ws82`` and ``vorp82`` projection columns.

    ``summer_league_player_seasons`` is created by the prior migration via
    ``SQLModel.metadata.create_all``, which reflects the *live* schema — so on a
    fresh database these columns already exist by the time this runs. Guard with
    ``IF NOT EXISTS`` so this is a no-op there while still adding the columns on
    databases whose table was created before the columns were defined.
    """
    op.execute(
        "ALTER TABLE summer_league_player_seasons "
        "ADD COLUMN IF NOT EXISTS ws82 DOUBLE PRECISION"
    )
    op.execute(
        "ALTER TABLE summer_league_player_seasons "
        "ADD COLUMN IF NOT EXISTS vorp82 DOUBLE PRECISION"
    )


def downgrade() -> None:
    """Drop the projection columns."""
    op.execute(
        "ALTER TABLE summer_league_player_seasons DROP COLUMN IF EXISTS vorp82"
    )
    op.execute(
        "ALTER TABLE summer_league_player_seasons DROP COLUMN IF EXISTS ws82"
    )
