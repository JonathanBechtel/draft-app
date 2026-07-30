"""Add dated version-flip columns to Summer League metric projections.

The parent table migration creates these tables from SQLModel metadata on a fresh
database. This revision is therefore defensive: it also upgrades databases that
already have the pre-version-flip tables in place.
"""

from typing import Mapping, Sequence, Union, cast

import sqlalchemy as sa
from alembic import op  # type: ignore[attr-defined]

revision: str = "c5d6e7f8a9b0"
down_revision: Union[str, None] = "c9d2e4f6a8b0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

REGISTRY_VERSION = "2026.07.1"
CALCULATION_VERSION = "2026.07.1"
VERSION_SEQUENCE = "summer_league_metric_version_seq"

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

_TABLES: dict[str, dict[str, object]] = {
    "summer_league_metric_contexts": {
        "old_unique": "uq_summer_league_metric_contexts_competition",
        "new_unique": "uq_summer_league_metric_contexts_competition_version",
        "current_index": "uq_summer_league_metric_contexts_current",
        "scope": ["competition_id"],
    },
    "summer_league_player_seasons": {
        "old_unique": "uq_summer_league_player_seasons_competition_player",
        "new_unique": "uq_summer_league_player_seasons_competition_player_version",
        "current_index": "uq_summer_league_player_seasons_current",
        "scope": ["competition_id", "player_id"],
    },
}


def _names(table: str) -> tuple[set[str], set[str]]:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns(table)}
    constraints = {
        constraint["name"]
        for constraint in inspector.get_unique_constraints(table)
        if constraint["name"]
    }
    return columns, constraints


def _add_version_columns(table: str) -> None:
    columns, _constraints = _names(table)
    if "version" not in columns:
        op.add_column(table, sa.Column("version", sa.Integer(), nullable=True))
    if "is_current" not in columns:
        op.add_column(table, sa.Column("is_current", sa.Boolean(), nullable=True))
    if "registry_version" not in columns:
        op.add_column(table, sa.Column("registry_version", sa.String(), nullable=True))
    if "calculation_version" not in columns:
        op.add_column(
            table, sa.Column("calculation_version", sa.String(), nullable=True)
        )
    if "as_of" not in columns:
        op.add_column(table, sa.Column("as_of", sa.DateTime(), nullable=True))

    op.execute(
        sa.text(
            f"UPDATE {table} SET version = COALESCE(version, 1), "
            "is_current = COALESCE(is_current, true), "
            "registry_version = COALESCE(registry_version, :registry), "
            "calculation_version = COALESCE(calculation_version, :calculation)"
        ).bindparams(registry=REGISTRY_VERSION, calculation=CALCULATION_VERSION)
    )

    for column in ("version", "is_current", "registry_version", "calculation_version"):
        op.alter_column(table, column, nullable=False)


def _create_constraints(table: str, spec: Mapping[str, object]) -> None:
    _columns, constraints = _names(table)
    old_unique = str(spec["old_unique"])
    new_unique = str(spec["new_unique"])
    scope_columns = cast(list[str], spec["scope"])
    if old_unique in constraints:
        op.drop_constraint(old_unique, table_name=table, type_="unique")
    if new_unique not in constraints:
        scope = scope_columns + ["version"]
        op.create_unique_constraint(new_unique, table, scope)

    inspector = sa.inspect(op.get_bind())
    indexes = {index["name"] for index in inspector.get_indexes(table) if index["name"]}
    current_index = str(spec["current_index"])
    index_is_invalid = False
    if current_index in indexes:
        index_is_invalid = (
            op.get_bind()
            .execute(_INDEX_VALIDITY_QUERY, {"index_name": current_index})
            .scalar_one_or_none()
            is False
        )
    if current_index not in indexes or index_is_invalid:
        # The metric projections are populated in production; build the partial
        # current-pointer index without holding a table write lock for the full
        # scan. The migration safety hook requires both halves of this pattern.
        with op.get_context().autocommit_block():
            if index_is_invalid:
                op.drop_index(
                    current_index,
                    table_name=table,
                    if_exists=True,
                    postgresql_concurrently=True,
                )
            op.create_index(
                current_index,
                table,
                scope_columns,
                unique=True,
                if_not_exists=True,
                postgresql_where=sa.text("is_current = true"),
                postgresql_concurrently=True,
            )


def _create_version_sequence() -> None:
    """Create and seed the atomic publication-version allocator."""
    bind = op.get_bind()
    op.execute(sa.text(f"CREATE SEQUENCE IF NOT EXISTS {VERSION_SEQUENCE}"))
    max_version = int(
        bind.execute(
            sa.text(
                "SELECT GREATEST("
                "COALESCE((SELECT MAX(version) FROM summer_league_metric_contexts), 0), "
                "COALESCE((SELECT MAX(version) FROM summer_league_player_seasons), 0)"
                ")"
            )
        ).scalar_one()
        or 0
    )
    bind.execute(
        sa.text(f"SELECT setval('{VERSION_SEQUENCE}', :value, :is_called)"),
        {"value": max(1, max_version), "is_called": max_version > 0},
    )


def upgrade() -> None:
    """Backfill legacy rows and enforce one current row per metric scope."""
    for table, spec in _TABLES.items():
        _add_version_columns(table)
        _create_constraints(table, spec)
    _create_version_sequence()


def downgrade() -> None:
    """Collapse history back to the legacy one-row-per-scope shape."""
    bind = op.get_bind()
    for table, spec in reversed(list(_TABLES.items())):
        inspector = sa.inspect(bind)
        indexes = {
            index["name"] for index in inspector.get_indexes(table) if index["name"]
        }
        current_index = str(spec["current_index"])
        if current_index in indexes:
            op.drop_index(current_index, table_name=table)

        _columns, constraints = _names(table)
        new_unique = str(spec["new_unique"])
        if new_unique in constraints:
            op.drop_constraint(new_unique, table_name=table, type_="unique")

        scope = cast(list[str], spec["scope"])
        op.execute(
            sa.text(
                f"DELETE FROM {table} a USING {table} b "
                f"WHERE a.id < b.id AND ({' AND '.join(f'a.{col} = b.{col}' for col in scope)})"
            )
        )
        old_unique = str(spec["old_unique"])
        _columns, constraints = _names(table)
        if old_unique not in constraints:
            op.create_unique_constraint(old_unique, table, scope)
        for column in (
            "as_of",
            "calculation_version",
            "registry_version",
            "is_current",
            "version",
        ):
            op.drop_column(table, column)
    op.execute(sa.text(f"DROP SEQUENCE IF EXISTS {VERSION_SEQUENCE}"))
