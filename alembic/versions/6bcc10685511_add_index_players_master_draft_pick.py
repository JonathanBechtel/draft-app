"""add index on players_master.draft_pick

Revision ID: 6bcc10685511
Revises: 1233bc724a9f
Create Date: 2026-06-22

"""

from typing import Sequence, Union

from alembic import op  # type: ignore[attr-defined]

revision: str = "6bcc10685511"
down_revision: Union[str, None] = "1233bc724a9f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_players_master_draft_pick",
        "players_master",
        ["draft_pick"],
    )


def downgrade() -> None:
    op.drop_index("ix_players_master_draft_pick", table_name="players_master")
