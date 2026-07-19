"""Add durable Summer League per-game batch-progress tracking.

Revision ID: n4o5p6q7r8s9
Revises: g7h8i9j0k1l2
Create Date: 2026-07-19
"""

from typing import Sequence, Union

from alembic import op  # type: ignore[attr-defined]
from sqlmodel import SQLModel

from app.schemas.summer_league_pipeline import SummerLeagueBatchProgress

revision: str = "n4o5p6q7r8s9"
down_revision: Union[str, None] = "g7h8i9j0k1l2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the per-game batch-progress tracking table."""
    SQLModel.metadata.create_all(
        bind=op.get_bind(),
        tables=[SummerLeagueBatchProgress.__table__],  # type: ignore[attr-defined]
    )


def downgrade() -> None:
    """Remove the per-game batch-progress tracking table."""
    SQLModel.metadata.drop_all(
        bind=op.get_bind(),
        tables=[SummerLeagueBatchProgress.__table__],  # type: ignore[attr-defined]
    )
