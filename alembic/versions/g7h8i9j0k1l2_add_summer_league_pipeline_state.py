"""Add durable Summer League cron coordination state.

Revision ID: g7h8i9j0k1l2
Revises: f3a1c2b4d5e6
Create Date: 2026-07-17
"""

from typing import Sequence, Union

from alembic import op  # type: ignore[attr-defined]
from sqlmodel import SQLModel

from app.schemas.summer_league_pipeline import SummerLeaguePipelineState

revision: str = "g7h8i9j0k1l2"
down_revision: Union[str, None] = "f3a1c2b4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the operational state projection."""
    SQLModel.metadata.create_all(
        bind=op.get_bind(),
        tables=[SummerLeaguePipelineState.__table__],  # type: ignore[attr-defined]
    )


def downgrade() -> None:
    """Remove the operational state projection."""
    SQLModel.metadata.drop_all(
        bind=op.get_bind(),
        tables=[SummerLeaguePipelineState.__table__],  # type: ignore[attr-defined]
    )
