"""Add SummerLeagueShotEvent and SummerLeaguePlayByPlayEvent tables.

Revision ID: 7a7804ad90cb
Revises: d8a7f6c5b4e3
Create Date: 2026-06-27

Foundation ticket for the shot-chart and PBP pipelines: two new event tables,
one row per shot attempt (SummerLeagueShotEvent) and one row per PBP event
(SummerLeaguePlayByPlayEvent). New tables only — created wholesale via
SQLModel.metadata.create_all per the repo migration convention.
"""

from typing import Sequence, Union

from alembic import op  # type: ignore[attr-defined]
from sqlmodel import SQLModel

from app.schemas.summer_league import SummerLeagueShotEvent, SummerLeaguePlayByPlayEvent

# revision identifiers, used by Alembic.
revision: str = "7a7804ad90cb"
down_revision: Union[str, None] = "d8a7f6c5b4e3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    SQLModel.metadata.create_all(
        bind=op.get_bind(),
        tables=[
            SummerLeagueShotEvent.__table__,  # type: ignore[attr-defined]
            SummerLeaguePlayByPlayEvent.__table__,  # type: ignore[attr-defined]
        ],
    )


def downgrade() -> None:
    SQLModel.metadata.drop_all(
        bind=op.get_bind(),
        tables=[
            SummerLeagueShotEvent.__table__,  # type: ignore[attr-defined]
            SummerLeaguePlayByPlayEvent.__table__,  # type: ignore[attr-defined]
        ],
    )
