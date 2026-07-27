"""Enforce at most one active Summer League metric fit.

Publication deactivates prior fits and then writes the new one. Within a single
transaction that is sound, but two overlapping unscoped rebuilds are not serialized
against each other: the hourly ingestion holds the Summer League writer lock while
``scripts/rebuild_sl_metrics.py`` takes no lock at all. A manual rebuild running
alongside the cron could therefore leave two rows with ``is_active = true``, after which
``_active_or_fresh_model_version()`` picks one arbitrarily by id.

Application-side ordering cannot fix that; the database has to. There is no scope column
here -- the fit is league-wide -- so the index is on a constant expression, which is the
standard way to spell "at most one row matching this predicate" in PostgreSQL. It is the
same guarantee ``summer_league_environment_profiles`` gets per ``scope_key``.

Autogenerate does not detect expression indexes, so this revision is hand-written
(the diff Alembic proposed contained only unrelated server_default noise).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]

# revision identifiers, used by Alembic.
revision: str = "b8e1c92a77d4"
down_revision: Union[str, None] = "3f8c1d47a9b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX_NAME = "uq_summer_league_metric_models_active"
TABLE_NAME = "summer_league_metric_models"


def upgrade() -> None:
    """Collapse any pre-existing multiple-active state, then enforce the invariant.

    The deactivation runs first because a unique index cannot be created over data that
    already violates it. Production holds exactly one row today (the wipe this change
    replaced guaranteed that), so this is defensive rather than corrective -- but a
    migration that fails on a database in a state the code could produce is not a
    migration, it is a trap. Newest id wins, matching the read path's
    ``ORDER BY id DESC``.

    ``if_not_exists=True``: the parent table is created by an earlier
    ``SQLModel.metadata.create_all`` migration that reflects the live model class, so a
    from-scratch bootstrap already has this index by the time this revision runs.
    """
    op.execute(
        sa.text(
            f"""
            UPDATE {TABLE_NAME}
               SET is_active = false
             WHERE is_active
               AND id <> (SELECT max(id) FROM {TABLE_NAME} WHERE is_active)
            """
        )
    )
    # discipline: migration-safety single-row table (production holds exactly one fit);
    # a non-concurrent build here takes a lock measured in milliseconds.
    op.create_index(
        INDEX_NAME,
        TABLE_NAME,
        [sa.text("(true)")],
        unique=True,
        postgresql_where=sa.text("is_active"),
        if_not_exists=True,
    )


def downgrade() -> None:
    """Drop the single-active index, leaving the rows themselves untouched."""
    op.drop_index(INDEX_NAME, table_name=TABLE_NAME, if_exists=True)
