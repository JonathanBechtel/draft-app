"""add index players_master draft_round

Revision ID: 6f111a4e928f
Revises: e6f7g8h9i0j1
Create Date: 2026-06-21

"""

from typing import Sequence, Union

from alembic import op  # type: ignore[attr-defined]

revision: str = "6f111a4e928f"
down_revision: Union[str, None] = "e6f7g8h9i0j1"
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
