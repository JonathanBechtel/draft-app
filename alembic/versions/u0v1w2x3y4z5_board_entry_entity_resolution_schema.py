"""Add entity-resolution columns to board_entries.

Revision ID: u0v1w2x3y4z5
Revises: t9u0v1w2x3y4
Create Date: 2026-05-26

Extends ``board_entries`` so the extraction pipeline can persist every
ranked position from an analyst's article — including positions where
the raw name did not resolve to a ``players_master`` row. Downstream
tickets (resolution cascade, admin candidate display) consume these
new columns.

Changes:
  - ``board_entries.player_id`` → NULLABLE (was NOT NULL).
  - Add ``raw_name VARCHAR NOT NULL DEFAULT ''``: verbatim AI-emitted name.
  - Add ``resolution_method resolution_method_enum NOT NULL DEFAULT 'UNRESOLVED'``:
    how (or whether) the name was resolved.
  - Add ``vector_candidates JSONB NULLABLE``: embedding nearest-neighbour
    candidates for admin review.
  - Add index on ``resolution_method`` for efficient unresolved queries.
  - Backfill existing rows: ``raw_name = players_master.display_name``,
    ``resolution_method = 'MANUAL'``.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "u0v1w2x3y4z5"
down_revision: Union[str, None] = "t9u0v1w2x3y4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


RESOLUTION_METHOD_ENUM_NAME = "resolution_method_enum"
RESOLUTION_METHOD_VALUES = ("EXACT", "ALIAS", "VECTOR", "MANUAL", "STUB", "UNRESOLVED")


def upgrade() -> None:
    # 1. Create the resolution_method enum type.
    resolution_enum = postgresql.ENUM(
        *RESOLUTION_METHOD_VALUES,
        name=RESOLUTION_METHOD_ENUM_NAME,
        create_type=False,
    )
    resolution_enum.create(op.get_bind(), checkfirst=True)

    # 2. Make player_id nullable (was NOT NULL).
    op.alter_column("board_entries", "player_id", nullable=True)

    # 3. Add raw_name (NOT NULL with empty-string default; backfilled below).
    op.add_column(
        "board_entries",
        sa.Column("raw_name", sa.String(), nullable=False, server_default=""),
    )

    # 4. Add resolution_method (NOT NULL with UNRESOLVED default).
    op.add_column(
        "board_entries",
        sa.Column(
            "resolution_method",
            resolution_enum,
            nullable=False,
            server_default="UNRESOLVED",
        ),
    )

    # 5. Add vector_candidates (nullable JSONB).
    op.add_column(
        "board_entries",
        sa.Column("vector_candidates", postgresql.JSONB, nullable=True),
    )

    # 6. Add index on resolution_method for efficient unresolved queries.
    op.create_index(
        "ix_board_entries_resolution_method",
        "board_entries",
        ["resolution_method"],
    )

    # 7. Backfill existing rows: set raw_name from the resolved player's
    #    display_name and tag them as MANUAL (a human previously verified them).
    #    ``players_master.display_name`` is nullable, so COALESCE to '' to keep
    #    the NOT NULL constraint satisfied even for stub players.
    op.execute(
        """
        UPDATE board_entries be
        SET
            raw_name          = COALESCE(pm.display_name, ''),
            resolution_method = 'MANUAL'
        FROM players_master pm
        WHERE be.player_id = pm.id
          AND be.resolution_method = 'UNRESOLVED'
        """
    )

    # 8. Remove the server default on raw_name now that backfill is done.
    #    Future rows must supply raw_name explicitly.
    op.alter_column("board_entries", "raw_name", server_default=None)


def downgrade() -> None:
    # Drop index before dropping column.
    op.drop_index("ix_board_entries_resolution_method", table_name="board_entries")

    op.drop_column("board_entries", "vector_candidates")
    op.drop_column("board_entries", "resolution_method")
    op.drop_column("board_entries", "raw_name")

    # Restore player_id as NOT NULL.
    # Any rows that have player_id=NULL at this point would violate the
    # constraint; callers should resolve or delete them first.
    op.alter_column("board_entries", "player_id", nullable=False)

    # Drop the enum type.
    bind = op.get_bind()
    sa.Enum(name=RESOLUTION_METHOD_ENUM_NAME).drop(bind, checkfirst=True)
