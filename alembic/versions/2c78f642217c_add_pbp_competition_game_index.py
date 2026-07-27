"""Add competition-leading index to summer_league_play_by_play_events.

Revision ID: 2c78f642217c
Revises: 9fae26346abb
Create Date: 2026-07-20

Ticket #643 (Competition Context Explorer hardening -- offline rebuild query
perf): the environment rebuild's ``_load_pbp`` step (in
``app/services/summer_league_environment_service.py``) filters play-by-play
events by ``competition_id`` (with the eligible-final-games join and
``GROUP BY competition_id, game_id``), but the only existing PBP index
(``ix_summer_league_pbp_events_game_period_event``) is led by ``game_id``.

`EXPLAIN (ANALYZE, BUFFERS)` against a Neon prod read branch confirmed this
falls back to a full ``Seq Scan`` on ``summer_league_play_by_play_events``:
one competition alone already holds ~80% of the table's ~40k rows, and every
other rebuild-path table (team/player game logs, participation, shot events)
already carries a competition-leading index for the equivalent access
pattern -- this table was the one gap.

This is an additive index on an existing table, so this migration uses a
targeted concurrent ``op.create_index``/``op.drop_index`` rather than
``SQLModel.metadata.create_all``/``drop_all`` per the repo migration
convention for existing-table changes. Concurrent DDL is essential here: a
regular ``CREATE INDEX`` queues a table lock that can hold subsequent public
reads behind a long-running ingestion transaction.
"""

from typing import Sequence, Union

from alembic import op  # type: ignore[attr-defined]
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = "2c78f642217c"
down_revision: Union[str, None] = "9fae26346abb"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX_NAME = "ix_summer_league_pbp_events_competition_game"
TABLE_NAME = "summer_league_play_by_play_events"

_INDEX_VALIDITY_QUERY = text(
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


def upgrade() -> None:
    """Add the ``(competition_id, game_id)`` index without blocking reads.

    ``if_not_exists=True``: the parent table is created by an earlier
    ``SQLModel.metadata.create_all`` migration that reflects the live model
    class, so a from-scratch bootstrap already has this index by the time
    this migration runs (see `16a524075c8e` for the same guard rationale).
    A failed ``CREATE INDEX CONCURRENTLY`` can leave an invalid catalog entry;
    remove that artifact first so ``IF NOT EXISTS`` cannot silently skip it.
    """
    with op.get_context().autocommit_block():
        index_is_valid = (
            op.get_bind()
            .execute(_INDEX_VALIDITY_QUERY, {"index_name": INDEX_NAME})
            .scalar_one_or_none()
        )
        if index_is_valid is False:
            op.drop_index(
                INDEX_NAME,
                table_name=TABLE_NAME,
                if_exists=True,
                postgresql_concurrently=True,
            )
        op.create_index(
            INDEX_NAME,
            TABLE_NAME,
            ["competition_id", "game_id"],
            if_not_exists=True,
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    """Drop the ``(competition_id, game_id)`` index without blocking reads."""
    with op.get_context().autocommit_block():
        op.drop_index(
            INDEX_NAME,
            table_name=TABLE_NAME,
            if_exists=True,
            postgresql_concurrently=True,
        )
