"""Add Summer League raw audit tables.

Revision ID: a6b7c8d9e0f1
Revises: z5a6b7c8d9e0
Create Date: 2026-06-09

"""

from typing import Sequence, Union

from alembic import op  # type: ignore[attr-defined]
from sqlmodel import SQLModel

from app.schemas.summer_league import SummerLeagueSourceDocument, SummerLeagueIngestionRun

# revision identifiers, used by Alembic.
revision: str = "a6b7c8d9e0f1"
down_revision: Union[str, None] = "z5a6b7c8d9e0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


SUMMER_LEAGUE_RAW_AUDIT_TABLES = [
    SummerLeagueIngestionRun.__table__,  # type: ignore[attr-defined]
    SummerLeagueSourceDocument.__table__,  # type: ignore[attr-defined]
]


def upgrade() -> None:
    """Create Summer League raw audit tables."""
    SQLModel.metadata.create_all(
        bind=op.get_bind(),
        tables=SUMMER_LEAGUE_RAW_AUDIT_TABLES,
    )


def downgrade() -> None:
    """Drop Summer League raw audit tables."""
    SQLModel.metadata.drop_all(
        bind=op.get_bind(),
        tables=list(reversed(SUMMER_LEAGUE_RAW_AUDIT_TABLES)),
    )
