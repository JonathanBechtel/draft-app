"""Add the event-day stamp used by Summer League metric trends.

``as_of`` is source currency and therefore cannot identify the event calendar
day for a historical snapshot. The nullable ``effective_day`` column carries
that day explicitly (Eastern). Existing rows remain NULL because publication
timestamps are job times, not event evidence; the archival backfill creates
cutoff-bound daily closes with truthful event dates.

The guards deliberately make this safe on databases initialized from SQLModel
metadata first: a fresh database already has the columns and indexes, while a
database upgraded from the previous head receives the same objects exactly
once.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]

# revision identifiers, used by Alembic.
revision: str = "9e7f1a2b3c4d"
down_revision: Union[str, None] = "8d6f2a1c9b4e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = (
    "summer_league_player_seasons",
    "summer_league_metric_contexts",
)
_INDEXES = {
    "summer_league_player_seasons": (
        "ix_summer_league_player_seasons_trend",
        "ix_summer_league_player_seasons_year_trend",
    ),
    "summer_league_metric_contexts": ("ix_summer_league_metric_contexts_trend",),
}
_INDEX_VALIDITY_QUERY = sa.text(
    """
    SELECT index_state.indisvalid
    FROM pg_index AS index_state
    JOIN pg_class AS index_class
      ON index_class.oid = index_state.indexrelid
    JOIN pg_namespace AS index_namespace
      ON index_namespace.oid = index_class.relnamespace
    WHERE index_namespace.nspname = current_schema()
      AND index_class.relname = :index_name
    """
)


def _column_exists(table_name: str) -> bool:
    """Return whether ``effective_day`` is already present on a table."""
    columns = sa.inspect(op.get_bind()).get_columns(table_name)
    return any(column["name"] == "effective_day" for column in columns)


def _index_exists(index_name: str, table_name: str) -> bool:
    """Return whether a trend index already exists on a table."""
    indexes = sa.inspect(op.get_bind()).get_indexes(table_name)
    return any(index["name"] == index_name for index in indexes)


def _index_is_invalid(index_name: str) -> bool:
    """Return whether a cancelled concurrent build left an INVALID index."""
    return (
        op.get_bind()
        .execute(_INDEX_VALIDITY_QUERY, {"index_name": index_name})
        .scalar_one_or_none()
        is False
    )


def upgrade() -> None:
    """Add and index ``effective_day`` on both projections."""
    for table_name in _TABLES:
        if not _column_exists(table_name):
            op.add_column(
                table_name,
                sa.Column("effective_day", sa.Date(), nullable=True),
            )
    # Indexes are declared on the SQLModel tables as well.  The projection
    # tables are populated in production, so use CIC inside an autocommit block
    # and recover an INVALID catalog entry left by an interrupted build.
    with op.get_context().autocommit_block():
        for table_name, index_names in _INDEXES.items():
            for index_name in index_names:
                if _index_is_invalid(index_name):
                    op.drop_index(
                        index_name,
                        table_name=table_name,
                        if_exists=True,
                        postgresql_concurrently=True,
                    )
                if _index_exists(index_name, table_name):
                    continue
                if index_name == "ix_summer_league_player_seasons_year_trend":
                    columns = ["year", "effective_day", "version", "published_at"]
                else:
                    columns = [
                        "competition_id",
                        "effective_day",
                        "version",
                        "published_at",
                    ]
                op.create_index(
                    index_name,
                    table_name,
                    columns,
                    if_not_exists=True,
                    postgresql_concurrently=True,
                )


def downgrade() -> None:
    """Drop trend indexes and the nullable event-day columns."""
    with op.get_context().autocommit_block():
        for table_name, index_names in _INDEXES.items():
            for index_name in index_names:
                op.drop_index(
                    index_name,
                    table_name=table_name,
                    if_exists=True,
                    postgresql_concurrently=True,
                )
    for table_name in _TABLES:
        if _column_exists(table_name):
            op.drop_column(table_name, "effective_day")
