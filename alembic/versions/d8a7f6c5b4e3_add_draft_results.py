"""Add draft_results table.

Revision ID: d8a7f6c5b4e3
Revises: c0ffee5a1b2c
Create Date: 2026-06-23

Records actual draft-night outcomes: one row per overall pick mapping the
selected player and team. Post-draft counterpart to ``big_board_consensus``
(pre-draft signal); joining the two powers the draft-recap reach/steal views.
New table → created wholesale via ``SQLModel.metadata.create_all`` per the repo
migration convention.
"""

from typing import Sequence, Union

from alembic import op  # type: ignore[attr-defined]
from sqlmodel import SQLModel

from app.schemas.draft_results import DraftResult

# revision identifiers, used by Alembic.
revision: str = "d8a7f6c5b4e3"
down_revision: Union[str, None] = "c0ffee5a1b2c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    SQLModel.metadata.create_all(
        bind=op.get_bind(),
        tables=[DraftResult.__table__],  # type: ignore[attr-defined]
    )


def downgrade() -> None:
    SQLModel.metadata.drop_all(
        bind=op.get_bind(),
        tables=[DraftResult.__table__],  # type: ignore[attr-defined]
    )
