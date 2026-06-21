"""add index players_master draft_round

Revision ID: a1b2c3d4e5f6
Revises: z5a6b7c8d9e0
Create Date: 2026-06-21

"""

from typing import Sequence, Union

from alembic import op  # type: ignore[attr-defined]

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "z5a6b7c8d9e0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_players_master_draft_round",
        "players_master",
        ["draft_round"],
    )


def downgrade() -> None:
    op.drop_index("ix_players_master_draft_round", table_name="players_master")
