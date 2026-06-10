"""Add Summer League player resolution reviews.

Revision ID: a9b8c7d6e5f4
Revises: c0ffee123456
Create Date: 2026-06-09

"""

from typing import Sequence, Union

from alembic import op  # type: ignore[attr-defined]
from sqlmodel import SQLModel

from app.schemas.summer_league import SummerLeaguePlayerResolutionReview

# revision identifiers, used by Alembic.
revision: str = "a9b8c7d6e5f4"
down_revision: Union[str, None] = "c0ffee123456"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the Summer League player-resolution review queue table."""
    SQLModel.metadata.create_all(
        bind=op.get_bind(),
        tables=[SummerLeaguePlayerResolutionReview.__table__],  # type: ignore[attr-defined]
    )


def downgrade() -> None:
    """Drop the Summer League player-resolution review queue table."""
    status_type = SummerLeaguePlayerResolutionReview.__table__.c.status.type  # type: ignore[attr-defined]
    SQLModel.metadata.drop_all(
        bind=op.get_bind(),
        tables=[SummerLeaguePlayerResolutionReview.__table__],  # type: ignore[attr-defined]
    )
    status_type.drop(op.get_bind(), checkfirst=True)
