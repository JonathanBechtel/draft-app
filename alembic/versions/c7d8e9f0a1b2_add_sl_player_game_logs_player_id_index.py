"""Add player_id index to summer_league_player_game_logs.

The player-detail Summer League section queries
``summer_league_player_game_logs`` by ``player_id`` alone. The existing
composite index ``(competition_id, player_id)`` cannot serve that filter
(leading column is ``competition_id``), so add a single-column index.

Revision ID: c7d8e9f0a1b2
Revises: b6c7d8e9f0a1
Create Date: 2026-06-13

"""

from typing import Sequence, Union

from alembic import op  # type: ignore[attr-defined]

# revision identifiers, used by Alembic.
revision: str = "c7d8e9f0a1b2"
down_revision: Union[str, None] = "b6c7d8e9f0a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX_NAME = "ix_summer_league_player_game_logs_player_id"
TABLE_NAME = "summer_league_player_game_logs"


def upgrade() -> None:
    """Create the single-column ``player_id`` index."""
    op.create_index(INDEX_NAME, TABLE_NAME, ["player_id"])


def downgrade() -> None:
    """Drop the single-column ``player_id`` index."""
    op.drop_index(INDEX_NAME, table_name=TABLE_NAME)
