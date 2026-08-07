"""Make organization_relationships append-only/bitemporal (ticket #798).

Revision ID: e4f5a6b7c8d9
Revises: 88e408e797c6
Create Date: 2026-08-07

As shipped in #781, ``organization_relationships`` carried
``UniqueConstraint(from_organization_id, to_organization_id, relationship_type)``
-- one row per triple forever -- while also carrying ``effective_start`` /
``effective_end``. Those two facts contradict each other: a relationship that
ended and later resumed cannot be recorded as two intervals. See the
``OrganizationRelationship`` class docstring for the full design rationale
(design B: mirror ``PlayerAffiliation``'s supersession shape) -- it is recorded
there, not here, so it stays next to the schema it describes.

This migration lands the structural half of that decision:

* drop ``uq_organization_relationships_from_to_type``;
* add the bitemporal stamps ``recorded_at`` (NOT NULL), ``supersedes_id``
  (self-FK), ``superseded_at``, ``retracted_at``;
* add ``ix_organization_relationships_supersedes_id``;
* add the partial unique index ``uq_organization_relationships_current`` on
  ``(from, to, type) WHERE superseded_at IS NULL AND retracted_at IS NULL AND
  effective_end IS NULL`` -- at most one *open* current edge per triple, any
  number of closed historical intervals.

**The table is empty** (#781 shipped the tables; #782 populated only
``organizations`` / ``team_programs``; no writer exists yet), so there is no
data migration and ``recorded_at`` can land NOT NULL without a backfill. The
``server_default`` on ``recorded_at`` is applied and then immediately dropped
so the migrated shape matches the SQLModel class exactly (which declares a
Python-side ``default_factory`` and no server default) -- otherwise
``alembic revision --autogenerate`` would report drift forever after.

Idempotency
-----------
Mandatory here, and not theoretical: ``b8c9d0e1f2a3`` creates this table via
``SQLModel.metadata.create_all`` against the **live** class, so a database built
from base already has the final shape -- new columns present, old constraint
absent, both indexes present -- by the time this revision runs. Every operation
below is therefore guarded on actual catalog state, exactly as
``c2d3e4f5a6b7`` does, and the whole revision is a clean no-op on a fresh
install while applying exactly once on a database upgraded from
``88e408e797c6``.

No new enum types are created or dropped. ``supersedes_id`` references this
same table (created back at ``b8c9d0e1f2a3``), so there is no forward reference
to a table created later in the chain -- the mistake that broke upgrade-from-base
in ``f8855e75c831``.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]

# revision identifiers, used by Alembic.
revision: str = "e4f5a6b7c8d9"
down_revision: Union[str, None] = "88e408e797c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE_NAME = "organization_relationships"

_OLD_UNIQUE_CONSTRAINT = "uq_organization_relationships_from_to_type"
_CURRENT_UNIQUE_INDEX = "uq_organization_relationships_current"
_SUPERSEDES_INDEX = "ix_organization_relationships_supersedes_id"

_CURRENT_INDEX_WHERE = sa.text(
    "superseded_at IS NULL AND retracted_at IS NULL AND effective_end IS NULL"
)

# Order matters: added front-to-back on upgrade, dropped back-to-front on downgrade.
_NEW_COLUMN_NAMES = ("recorded_at", "supersedes_id", "superseded_at", "retracted_at")


def _new_columns() -> list[sa.Column]:  # type: ignore[type-arg]
    """Build the four bitemporal columns.

    Constructed fresh per call rather than held in a module constant: a
    ``sa.Column`` binds to a table the first time it is used, so a shared
    instance is not safe to reuse across operations.

    The self-FK on ``supersedes_id`` is deliberately left unnamed so Postgres
    auto-names it ``organization_relationships_supersedes_id_fkey`` -- exactly
    what ``SQLModel``'s ``foreign_key=`` produces via ``create_all``. An explicit
    name here would make a migrated database differ from a fresh one and show up
    as permanent autogenerate drift.
    """
    return [
        sa.Column(
            "recorded_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "supersedes_id",
            sa.Integer(),
            sa.ForeignKey(f"{TABLE_NAME}.id"),
            nullable=True,
        ),
        sa.Column("superseded_at", sa.DateTime(), nullable=True),
        sa.Column("retracted_at", sa.DateTime(), nullable=True),
    ]

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


def _column_exists(column_name: str) -> bool:
    """Return whether ``column_name`` is already present on the table."""
    columns = sa.inspect(op.get_bind()).get_columns(TABLE_NAME)
    return any(column["name"] == column_name for column in columns)


def _unique_constraint_exists(constraint_name: str) -> bool:
    """Return whether a named UNIQUE *constraint* exists on the table.

    Deliberately distinct from ``_index_exists``: Postgres backs a unique
    constraint with an index of the same name, so an index-only probe cannot
    tell "constraint" from "plain unique index" and the drop/recreate pair
    would pick the wrong DDL.
    """
    inspector = sa.inspect(op.get_bind())
    return any(
        constraint["name"] == constraint_name
        for constraint in inspector.get_unique_constraints(TABLE_NAME)
    )


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
    """Replace the blanket unique constraint with bitemporal stamps + a current-row index."""
    if _unique_constraint_exists(_OLD_UNIQUE_CONSTRAINT):
        op.drop_constraint(_OLD_UNIQUE_CONSTRAINT, TABLE_NAME, type_="unique")

    for column in _new_columns():
        if not _column_exists(column.name):
            op.add_column(TABLE_NAME, column)
            # The class declares a Python-side default_factory and no server
            # default; keep the migrated shape identical so autogenerate stays
            # empty and no future insert can silently fall back to now().
            if column.server_default is not None:
                op.alter_column(TABLE_NAME, column.name, server_default=None)

    # Index DDL is kept inline (not factored into a helper) so the migration-safety
    # checker (scripts/check_migration_safety.py) can see both calls lexically
    # inside the autocommit block it requires for CONCURRENTLY.
    with op.get_context().autocommit_block():
        if _index_is_invalid(_SUPERSEDES_INDEX):
            op.drop_index(
                _SUPERSEDES_INDEX,
                table_name=TABLE_NAME,
                if_exists=True,
                postgresql_concurrently=True,
            )
        if not _index_exists(_SUPERSEDES_INDEX):
            op.create_index(
                _SUPERSEDES_INDEX,
                TABLE_NAME,
                ["supersedes_id"],
                if_not_exists=True,
                postgresql_concurrently=True,
            )

        if _index_is_invalid(_CURRENT_UNIQUE_INDEX):
            op.drop_index(
                _CURRENT_UNIQUE_INDEX,
                table_name=TABLE_NAME,
                if_exists=True,
                postgresql_concurrently=True,
            )
        if not _index_exists(_CURRENT_UNIQUE_INDEX):
            op.create_index(
                _CURRENT_UNIQUE_INDEX,
                TABLE_NAME,
                [
                    "from_organization_id",
                    "to_organization_id",
                    "relationship_type",
                ],
                unique=True,
                if_not_exists=True,
                postgresql_where=_CURRENT_INDEX_WHERE,
                postgresql_concurrently=True,
            )


def downgrade() -> None:
    """Restore the blanket unique constraint and drop the bitemporal stamps.

    **This downgrade is effectively one-way once the table holds more than one
    interval per ``(from_organization_id, to_organization_id, relationship_type)``
    triple.** The restored blanket unique constraint cannot represent the history
    the upgrade exists to enable, so its ``CREATE UNIQUE INDEX`` fails with
    ``could not create unique index "uq_organization_relationships_from_to_type"``
    and the whole downgrade rolls back atomically -- the version stays at
    ``e4f5a6b7c8d9``, the four columns stay, and every row stays. Note this bites
    on *any* second interval, including a closed historical one: a closed row still
    has ``superseded_at IS NULL``, so nothing about supersession exempts it.

    That refusal is intended, not a bug to route around. A migration that made the
    data fit the old constraint would have to delete or merge assertions, which is
    exactly the history destruction ticket #798 removed. Do not add a dedup step.
    The correct recovery is to reconcile or archive the historical rows first,
    deliberately and with an operator's eyes on it, and only then downgrade.

    A non-issue today: the table is empty in production (#781 shipped it, #782
    populated only ``organizations`` / ``team_programs``, and no writer exists
    yet), so the downgrade is clean and fully reversible as things stand.
    """
    with op.get_context().autocommit_block():
        op.drop_index(
            _CURRENT_UNIQUE_INDEX,
            table_name=TABLE_NAME,
            if_exists=True,
            postgresql_concurrently=True,
        )
        op.drop_index(
            _SUPERSEDES_INDEX,
            table_name=TABLE_NAME,
            if_exists=True,
            postgresql_concurrently=True,
        )

    # Reverse add order; drop_column removes the self-FK constraint with it.
    for column_name in reversed(_NEW_COLUMN_NAMES):
        if _column_exists(column_name):
            op.drop_column(TABLE_NAME, column_name)

    if not _unique_constraint_exists(_OLD_UNIQUE_CONSTRAINT):
        op.create_unique_constraint(
            _OLD_UNIQUE_CONSTRAINT,
            TABLE_NAME,
            ["from_organization_id", "to_organization_id", "relationship_type"],
        )
