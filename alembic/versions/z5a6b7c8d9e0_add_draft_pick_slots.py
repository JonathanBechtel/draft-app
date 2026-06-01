"""Add draft_pick_slots reference table.

Revision ID: z5a6b7c8d9e0
Revises: 3c74bd15d744
Create Date: 2026-06-01

Canonical draft-order reference: one row per overall pick of a draft year
recording the owning team (and original team / trade note when traded). New
table → created wholesale via ``SQLModel.metadata.create_all`` per the repo
migration convention. See ``docs/draft_order_reference_plan.md``.
"""

from typing import Sequence, Union

from alembic import op  # type: ignore[attr-defined]
from sqlmodel import SQLModel

from app.schemas.draft_pick_slots import DraftPickSlot

# revision identifiers, used by Alembic.
revision: str = "z5a6b7c8d9e0"
down_revision: Union[str, None] = "3c74bd15d744"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    SQLModel.metadata.create_all(
        bind=op.get_bind(),
        tables=[DraftPickSlot.__table__],  # type: ignore[attr-defined]
    )


def downgrade() -> None:
    SQLModel.metadata.drop_all(
        bind=op.get_bind(),
        tables=[DraftPickSlot.__table__],  # type: ignore[attr-defined]
    )
