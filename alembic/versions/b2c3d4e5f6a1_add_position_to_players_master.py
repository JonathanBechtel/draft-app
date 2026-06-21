"""add position to players_master with index

Revision ID: b2c3d4e5f6a1
Revises: a1b2c3d4e5f6
Create Date: 2026-06-21

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]

revision: str = "b2c3d4e5f6a1"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "players_master",
        sa.Column("position", sa.String(), nullable=True),
    )
    op.create_index(
        "ix_players_master_position",
        "players_master",
        ["position"],
    )


def downgrade() -> None:
    op.drop_index("ix_players_master_position", table_name="players_master")
    op.drop_column("players_master", "position")
