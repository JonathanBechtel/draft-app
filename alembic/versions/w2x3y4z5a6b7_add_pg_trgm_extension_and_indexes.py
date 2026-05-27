"""Add pg_trgm extension and trigram indexes for lexical candidate search.

Revision ID: w2x3y4z5a6b7
Revises: v1w2x3y4z5a6
Create Date: 2026-05-26

Enables the ``pg_trgm`` extension and creates GIN trigram indexes on
``players_master.display_name`` and ``player_aliases.full_name``.  These
indexes support the ``similarity()`` / ``%`` operator used by the hybrid
candidate-matching path in ``player_search_service.find_candidate_players``,
which blends a trigram lexical score with the existing cosine-distance vector
score to surface bare-surname queries (e.g. "mara" → Aday Mara).

This migration:
  - Creates the pg_trgm extension (CREATE EXTENSION IF NOT EXISTS pg_trgm).
  - Creates a GIN trigram index on players_master.display_name.
  - Creates a GIN trigram index on player_aliases.full_name.
"""

from typing import Sequence, Union

from alembic import op  # type: ignore[attr-defined]

# revision identifiers, used by Alembic.
revision: str = "w2x3y4z5a6b7"
down_revision: Union[str, None] = "v1w2x3y4z5a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Enable pg_trgm and add GIN trigram indexes for lexical search."""
    # 1. Enable the pg_trgm extension.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # 2. GIN trigram index on players_master.display_name.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_players_master_display_name_trgm "
        "ON players_master USING GIN (display_name gin_trgm_ops)"
    )

    # 3. GIN trigram index on player_aliases.full_name.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_player_aliases_full_name_trgm "
        "ON player_aliases USING GIN (full_name gin_trgm_ops)"
    )


def downgrade() -> None:
    """Drop trigram indexes.

    The pg_trgm extension is intentionally left in place: ``CREATE
    EXTENSION IF NOT EXISTS pg_trgm`` in upgrade may have been a no-op
    (the extension may pre-exist or be shared with other objects), and
    unconditionally dropping it can fail on dependent objects or remove
    infrastructure this migration did not exclusively own.
    """
    op.execute("DROP INDEX IF EXISTS ix_player_aliases_full_name_trgm")
    op.execute("DROP INDEX IF EXISTS ix_players_master_display_name_trgm")
