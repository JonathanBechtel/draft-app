"""add position to players_master with index

Revision ID: 1233bc724a9f
Revises: 6f111a4e928f
Create Date: 2026-06-21

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]

revision: str = "1233bc724a9f"
down_revision: Union[str, None] = "6f111a4e928f"
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
