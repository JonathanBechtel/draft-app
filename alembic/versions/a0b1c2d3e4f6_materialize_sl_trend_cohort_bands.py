"""Materialize Summer League trend cohort bands on projection rows.

Revision ID: a0b1c2d3e4f6
Revises: 9e7f1a2b3c4d
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a0b1c2d3e4f6"
down_revision: Union[str, None] = "9e7f1a2b3c4d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE_NAME = "summer_league_player_seasons"
COLUMNS = (
    "trend_competition_bands",
    "trend_season_bands",
)


def _column_names() -> set[str]:
    """Return columns already present on the projection table."""
    return {
        str(column["name"])
        for column in sa.inspect(op.get_bind()).get_columns(TABLE_NAME)
    }


def upgrade() -> None:
    """Add nullable JSON projections populated by subsequent rebuilds/backfills."""
    existing = _column_names()
    for column_name in COLUMNS:
        if column_name not in existing:
            op.add_column(
                TABLE_NAME,
                sa.Column(
                    column_name, postgresql.JSONB(astext_type=sa.Text()), nullable=True
                ),
            )


def downgrade() -> None:
    """Remove the materialized cohort-band payloads."""
    existing = _column_names()
    for column_name in reversed(COLUMNS):
        if column_name in existing:
            op.drop_column(TABLE_NAME, column_name)
