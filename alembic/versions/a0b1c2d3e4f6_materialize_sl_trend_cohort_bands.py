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

PLAYER_TABLE = "summer_league_player_seasons"
CONTEXT_TABLE = "summer_league_metric_contexts"
PLAYER_COLUMNS: dict[str, sa.types.TypeEngine] = {
    "trend_competition_bands": postgresql.JSONB(astext_type=sa.Text()),
    "trend_season_bands": postgresql.JSONB(astext_type=sa.Text()),
    "trend_season_as_of": sa.DateTime(),
}


def _column_names(table_name: str) -> set[str]:
    """Return columns already present on the projection table."""
    return {
        str(column["name"])
        for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def upgrade() -> None:
    """Add nullable JSON projections populated by subsequent rebuilds/backfills."""
    existing = _column_names(PLAYER_TABLE)
    for column_name, column_type in PLAYER_COLUMNS.items():
        if column_name not in existing:
            op.add_column(
                PLAYER_TABLE,
                sa.Column(column_name, column_type, nullable=True),
            )
    for table_name in (PLAYER_TABLE, CONTEXT_TABLE):
        if "is_archival" not in _column_names(table_name):
            op.add_column(
                table_name,
                sa.Column(
                    "is_archival",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.false(),
                ),
            )


def downgrade() -> None:
    """Remove the materialized cohort-band payloads."""
    for table_name in (CONTEXT_TABLE, PLAYER_TABLE):
        if "is_archival" in _column_names(table_name):
            op.drop_column(table_name, "is_archival")
    existing = _column_names(PLAYER_TABLE)
    for column_name in reversed(PLAYER_COLUMNS):
        if column_name in existing:
            op.drop_column(PLAYER_TABLE, column_name)
