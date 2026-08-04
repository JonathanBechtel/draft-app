"""Add team_program_id to summer_league_team_entries.

Ticket #784 (phase-4 journey-graph conversion, T-current): retarget
``summer_league_team_entries`` at the generic org model additively, mirroring
T4 (#783)'s ``player_affiliations`` migration exactly. Per phase-4 spec §5.1
decision D3, ``team_program_id`` lands as a **nullable** column beside
``nba_team_id`` -- both are retained, no row is repointed, and
``nba_stats_team_id`` (the provider-specific id backing the existing unique
constraint) is untouched. Reads resolve through
``app.services.player_affiliation.resolve_team_target``, which prefers
``team_program_id`` and falls back to ``nba_team_id``.

The data backfill (existing ``nba_team_id`` -> the matching ``team_programs``
row, joined via the ``nba-<slug>`` natural key from
``scripts/populate_org_model_from_nba_teams.py``) is a separate operator
script, ``scripts/backfill_sl_team_entry_team_program.py`` -- this migration
is structural only.

The guards make this safe on databases initialized from SQLModel metadata
first: a fresh database (``SQLModel.metadata.create_all`` against the
current, already-updated model class) already has the column and index,
while a database upgraded from the previous head receives them exactly once.
``summer_league_team_entries`` is populated in production, so the index is
built concurrently, outside the migration transaction, per this repo's
migration-safety discipline (incident #669).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]

# revision identifiers, used by Alembic.
revision: str = "f8855e75c831"
down_revision: Union[str, None] = "c2d3e4f5a6b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE_NAME = "summer_league_team_entries"
COLUMN_NAME = "team_program_id"

_INDEX_NAME = "ix_summer_league_team_entries_team_program_id"

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


def _column_exists() -> bool:
    """Return whether ``team_program_id`` is already present on the table."""
    columns = sa.inspect(op.get_bind()).get_columns(TABLE_NAME)
    return any(column["name"] == COLUMN_NAME for column in columns)


def _index_exists(index_name: str) -> bool:
    """Return whether ``index_name`` already exists on the table."""
    indexes = sa.inspect(op.get_bind()).get_indexes(TABLE_NAME)
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
    """Add the nullable ``team_program_id`` FK and its supporting index."""
    if not _column_exists():
        op.add_column(
            TABLE_NAME,
            sa.Column(
                COLUMN_NAME,
                sa.Integer(),
                sa.ForeignKey("team_programs.id"),
                nullable=True,
            ),
        )

    # Index DDL is kept inline (not factored into a helper) so the migration-safety
    # checker (scripts/check_migration_safety.py) can see the call lexically
    # inside the autocommit block it requires for CONCURRENTLY.
    with op.get_context().autocommit_block():
        if _index_is_invalid(_INDEX_NAME):
            op.drop_index(
                _INDEX_NAME,
                table_name=TABLE_NAME,
                if_exists=True,
                postgresql_concurrently=True,
            )
        if not _index_exists(_INDEX_NAME):
            op.create_index(
                _INDEX_NAME,
                TABLE_NAME,
                [COLUMN_NAME],
                if_not_exists=True,
                postgresql_concurrently=True,
            )


def downgrade() -> None:
    """Drop the supporting index and the ``team_program_id`` column."""
    with op.get_context().autocommit_block():
        op.drop_index(
            _INDEX_NAME,
            table_name=TABLE_NAME,
            if_exists=True,
            postgresql_concurrently=True,
        )

    if _column_exists():
        op.drop_column(TABLE_NAME, COLUMN_NAME)
