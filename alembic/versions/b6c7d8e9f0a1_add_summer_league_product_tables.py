"""Add Summer League product tables.

Revision ID: b6c7d8e9f0a1
Revises: a6b7c8d9e0f1
Create Date: 2026-06-09

"""

from typing import Sequence, Union

from alembic import op  # type: ignore[attr-defined]
from sqlmodel import SQLModel

from app.schemas.summer_league import (
    SummerLeagueEdition,
    SummerLeagueGame,
    SummerLeaguePlayerGameLog,
    SummerLeagueSourceRecord,
    SummerLeagueTeamEntry,
    SummerLeagueTeamGameLog,
)

# revision identifiers, used by Alembic.
revision: str = "b6c7d8e9f0a1"
down_revision: Union[str, None] = "a6b7c8d9e0f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


SUMMER_LEAGUE_PRODUCT_TABLES = [
    SummerLeagueEdition.__table__,  # type: ignore[attr-defined]
    SummerLeagueTeamEntry.__table__,  # type: ignore[attr-defined]
    SummerLeagueGame.__table__,  # type: ignore[attr-defined]
    SummerLeagueSourceRecord.__table__,  # type: ignore[attr-defined]
    SummerLeagueTeamGameLog.__table__,  # type: ignore[attr-defined]
    SummerLeaguePlayerGameLog.__table__,  # type: ignore[attr-defined]
]


def upgrade() -> None:
    """Create Summer League product tables."""
    SQLModel.metadata.create_all(
        bind=op.get_bind(),
        tables=SUMMER_LEAGUE_PRODUCT_TABLES,
    )


def downgrade() -> None:
    """Drop Summer League product tables."""
    SQLModel.metadata.drop_all(
        bind=op.get_bind(),
        tables=list(reversed(SUMMER_LEAGUE_PRODUCT_TABLES)),
    )
