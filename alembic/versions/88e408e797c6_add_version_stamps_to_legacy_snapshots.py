"""Add registry_version/calculation_version/as_of to legacy snapshot tables.

Ticket #785 (phase-4 journey-graph conversion, T5): closes Phase 3's deferred
legacy-stamp gap. ``metric_snapshots`` and ``player_image_snapshots`` already
carry ``version`` + ``is_current`` but predate
``app.schemas.base.DatedVersionMixin`` (see that class's docstring for what
the three version stamps mean and why they must never be conflated). Both
model classes now inherit the mixin (``app/schemas/metrics.py``,
``app/schemas/image_snapshots.py``), so this migration lands the three
missing columns additively:

* ``registry_version`` (NOT NULL) -- the metric-definition/prompt version.
* ``calculation_version`` (NOT NULL) -- the pipeline/mechanics version.
* ``as_of`` (nullable) -- source currency (P4); no backfill value exists for
  historical rows, so it lands NULL like every other adopter's default.

Backfill: existing rows predate registry/calculation versioning entirely, so
backfilling them with any real identifier would fabricate a formula or
prompt version they were never actually built under -- worse than an honest
gap. Both NOT NULL columns land with a temporary ``server_default`` of
``'unknown'`` (``app.schemas.metrics.LEGACY_VERSION_SENTINEL`` /
``app.schemas.image_snapshots.LEGACY_VERSION_SENTINEL``) so the backfill and
the NOT NULL constraint can both be satisfied in one statement, then the
server default is dropped immediately after: publishers now always pass
explicit values (#785), and a lingering default would let a future insert
silently fall back to the sentinel instead of failing loudly.

Neither table gains a new partial-unique index here -- ``uq_metric_snapshots_current``
and ``uq_image_snapshots_current`` already exist (verified against
``app/schemas/metrics.py`` / ``app/schemas/image_snapshots.py`` and the
original migrations that created them), so this ticket's additive work is
column-only.

The guards make this safe on databases initialized from SQLModel metadata
first: a fresh database (``SQLModel.metadata.create_all`` against the
current, already-updated model classes) already has all three columns per
table, while a database upgraded from the previous head receives them
exactly once. No index DDL runs in this migration, so no concurrent-build /
autocommit-block handling is needed (``scripts/check_migration_safety.py``
only gates ``create_index``/``drop_index``).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]

# revision identifiers, used by Alembic.
revision: str = "88e408e797c6"
down_revision: Union[str, None] = "f8855e75c831"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ("metric_snapshots", "player_image_snapshots")
_SENTINEL = "unknown"
_NOT_NULL_COLUMNS = ("registry_version", "calculation_version")
_NULLABLE_COLUMNS = ("as_of",)


def _existing_columns(table_name: str) -> set[str]:
    """Return the column names ``table_name`` currently has."""
    return {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def upgrade() -> None:
    """Add the three DatedVersionMixin columns to each legacy snapshot table."""
    for table_name in _TABLES:
        existing = _existing_columns(table_name)

        for column_name in _NOT_NULL_COLUMNS:
            if column_name in existing:
                continue
            op.add_column(
                table_name,
                sa.Column(
                    column_name,
                    sa.String(),
                    nullable=False,
                    server_default=_SENTINEL,
                ),
            )
            # Drop the default once existing rows are backfilled: publishers
            # must pass an explicit value going forward, never fall back to
            # the sentinel silently.
            op.alter_column(table_name, column_name, server_default=None)

        for column_name in _NULLABLE_COLUMNS:
            if column_name in existing:
                continue
            op.add_column(
                table_name,
                sa.Column(column_name, sa.DateTime(), nullable=True),
            )


def downgrade() -> None:
    """Drop the three columns from each legacy snapshot table, if present."""
    for table_name in _TABLES:
        existing = _existing_columns(table_name)
        for column_name in (*_NOT_NULL_COLUMNS, *_NULLABLE_COLUMNS):
            if column_name in existing:
                op.drop_column(table_name, column_name)
