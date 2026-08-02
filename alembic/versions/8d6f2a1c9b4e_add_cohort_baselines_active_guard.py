"""Enforce one active Summer League cohort baseline per cohort.

The desk publication path keeps historical baseline versions in place and flips the
newest row for each ``cohort_key`` to ``is_active``.  The old schema only indexed
``(cohort_key, is_active)`` and therefore allowed overlapping publications to expose
two active rows for the same cohort.  This revision replaces that advisory index with
a partial unique index.

Existing databases may already contain duplicate active rows, so the upgrade first
keeps the newest row (``computed_at`` descending, ``id`` as a deterministic tie-break)
and deactivates the rest.  The index is built concurrently because this table is
populated in production.  Both directions are defensive against a fresh
``create_all`` database and an interrupted ``CREATE INDEX CONCURRENTLY``: an INVALID
catalog entry is removed before retrying the build.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]

# revision identifiers, used by Alembic.
revision: str = "8d6f2a1c9b4e"
down_revision: Union[str, None] = "715a9c0d1e2f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE_NAME = "summer_league_cohort_baselines"
ACTIVE_INDEX_NAME = "uq_summer_league_cohort_baselines_active"
LEGACY_INDEX_NAME = "ix_summer_league_cohort_baselines_cohort_active"

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

_DEDUPLICATE_ACTIVE_ROWS = sa.text(
    f"""
    WITH ranked AS (
        SELECT id,
               ROW_NUMBER() OVER (
                   PARTITION BY cohort_key
                   ORDER BY computed_at DESC, id DESC
               ) AS row_number
        FROM {TABLE_NAME}
        WHERE is_active
    )
    UPDATE {TABLE_NAME} AS baseline
       SET is_active = FALSE
      FROM ranked
     WHERE baseline.id = ranked.id
       AND ranked.row_number > 1
    """
)


def _index_is_invalid(index_name: str) -> bool:
    """Return whether a catalog entry is known to be an INVALID index."""
    return (
        op.get_bind()
        .execute(_INDEX_VALIDITY_QUERY, {"index_name": index_name})
        .scalar_one_or_none()
        is False
    )


def upgrade() -> None:
    """Deduplicate active rows and install the partial unique guard."""
    # Run this before entering the autocommit block.  Alembic commits the
    # deactivation transaction before the concurrent DDL starts, so a retry sees a
    # clean data set even if a lock timeout interrupts index creation.
    op.execute(_DEDUPLICATE_ACTIVE_ROWS)

    with op.get_context().autocommit_block():
        # The previous non-unique index is superseded by the partial unique one.  A
        # fresh create_all database has no legacy index, hence IF EXISTS.
        op.drop_index(
            LEGACY_INDEX_NAME,
            table_name=TABLE_NAME,
            if_exists=True,
            postgresql_concurrently=True,
        )

        # A cancelled concurrent build leaves an INVALID index behind.  PostgreSQL's
        # IF NOT EXISTS treats that catalog entry as present, so remove it explicitly
        # before retrying.
        if _index_is_invalid(ACTIVE_INDEX_NAME):
            op.drop_index(
                ACTIVE_INDEX_NAME,
                table_name=TABLE_NAME,
                if_exists=True,
                postgresql_concurrently=True,
            )

        op.create_index(
            ACTIVE_INDEX_NAME,
            TABLE_NAME,
            ["cohort_key"],
            unique=True,
            if_not_exists=True,
            postgresql_where=sa.text("is_active"),
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    """Restore the legacy advisory index while preserving all rows."""
    with op.get_context().autocommit_block():
        op.drop_index(
            ACTIVE_INDEX_NAME,
            table_name=TABLE_NAME,
            if_exists=True,
            postgresql_concurrently=True,
        )

        if _index_is_invalid(LEGACY_INDEX_NAME):
            op.drop_index(
                LEGACY_INDEX_NAME,
                table_name=TABLE_NAME,
                if_exists=True,
                postgresql_concurrently=True,
            )

        op.create_index(
            LEGACY_INDEX_NAME,
            TABLE_NAME,
            ["cohort_key", "is_active"],
            if_not_exists=True,
            postgresql_concurrently=True,
        )
