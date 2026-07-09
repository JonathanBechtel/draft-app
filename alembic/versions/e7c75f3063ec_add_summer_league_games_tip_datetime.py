"""Add summer_league_games.tip_datetime and IN_PROGRESS game status.

Existing-table migration (behavior spec §10 "Data prerequisites"): the Desk
state machine and Morning Card cannot exist without tip times, and the
"live" state needs a distinct in-progress status. Never drops/recreates
summer_league_games -- adds a nullable column plus one enum value.

Revision ID: e7c75f3063ec
Revises: b2f10d7f542d
Create Date: 2026-07-09
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]

# revision identifiers, used by Alembic.
revision: str = "e7c75f3063ec"
down_revision: Union[str, None] = "b2f10d7f542d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add nullable tip_datetime and the IN_PROGRESS game status value."""
    op.add_column(
        "summer_league_games",
        sa.Column("tip_datetime", sa.DateTime(), nullable=True),
    )

    # Postgres enum ADD VALUE cannot run inside a transaction block; isolate it
    # in its own autocommit block (matches bc9443ccd2b6, l1m2n3o4p5q6).
    with op.get_context().autocommit_block():
        op.execute(
            "ALTER TYPE summer_league_game_status_enum "
            "ADD VALUE IF NOT EXISTS 'in_progress'"
        )


def downgrade() -> None:
    """Drop tip_datetime; leave the IN_PROGRESS enum value in place.

    Postgres cannot drop a single enum value (`ALTER TYPE ... DROP VALUE`
    does not exist), and rows may already reference IN_PROGRESS by the time
    this runs. Recreating the type without the value would require rewriting
    every dependent column/constraint just to reverse an additive,
    backwards-compatible change. This mirrors the repo's existing no-op
    precedent for enum-value downgrades (bc9443ccd2b6, l1m2n3o4p5q6): the
    value is inert for any code path that no longer writes it.
    """
    op.drop_column("summer_league_games", "tip_datetime")
