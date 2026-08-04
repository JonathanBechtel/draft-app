"""Add Summer League advanced-metrics materialized tables.

Revision ID: d4e5f6a7b8c9
Revises: b3d9f17c2a84
Create Date: 2026-06-15

"""

from typing import Sequence, Union

from alembic import op  # type: ignore[attr-defined]
from sqlmodel import SQLModel

from app.schemas.summer_league_metrics import (
    SummerLeagueMetricContext,
    SummerLeagueMetricModel,
    SummerLeagueDerivedAgg,
)

# revision identifiers, used by Alembic.
revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, None] = "b3d9f17c2a84"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


SUMMER_LEAGUE_METRIC_TABLES = [
    SummerLeagueMetricModel.__table__,  # type: ignore[attr-defined]
    SummerLeagueMetricContext.__table__,  # type: ignore[attr-defined]
    SummerLeagueDerivedAgg.__table__,  # type: ignore[attr-defined]
]


def upgrade() -> None:
    """Create the Summer League advanced-metrics tables."""
    SQLModel.metadata.create_all(
        bind=op.get_bind(),
        tables=SUMMER_LEAGUE_METRIC_TABLES,
    )


def downgrade() -> None:
    """Drop the Summer League advanced-metrics tables."""
    SQLModel.metadata.drop_all(
        bind=op.get_bind(),
        tables=list(reversed(SUMMER_LEAGUE_METRIC_TABLES)),
    )
