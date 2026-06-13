"""Add player_id index to summer_league_player_game_logs.

The player-detail Summer League section queries
``summer_league_player_game_logs`` by ``player_id`` alone. The existing
composite index ``(competition_id, player_id)`` cannot serve that filter
(leading column is ``competition_id``), so add a single-column index.

Revision ID: c7d8e9f0a1b2
Revises: a9b8c7d6e5f4
Create Date: 2026-06-13

"""

from typing import Sequence, Union

from alembic import op  # type: ignore[attr-defined]

# revision identifiers, used by Alembic.
revision: str = "c7d8e9f0a1b2"
down_revision: Union[str, None] = "a9b8c7d6e5f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX_NAME = "ix_summer_league_player_game_logs_player_id"
TABLE_NAME = "summer_league_player_game_logs"


def upgrade() -> None:
    """Create the single-column ``player_id`` index.

    Idempotent: on a fresh database the table is created from the live SQLModel
    metadata (which already declares this index), so ``IF NOT EXISTS`` no-ops;
    on an existing database that predates the index, it is created.
    """
    op.execute(f'CREATE INDEX IF NOT EXISTS {INDEX_NAME} ON {TABLE_NAME} (player_id)')


def downgrade() -> None:
    """Drop the single-column ``player_id`` index."""
    op.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")
