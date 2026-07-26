"""Reflective coverage test: every registered player column must be indexed.

Failure this descends from
--------------------------
Registering a child table with the merge service is what makes it *scanned*. Both the
merge path (``_merge_child_table``'s ``UPDATE ... WHERE col = :discard_id``) and the
safe-delete guard (``count_inbound_references``) address every registered table by its
player column, and Postgres's RESTRICT check does the same when the ``players_master``
row is finally deleted. A foreign key does **not** create an index, so a registration
without one turns each of those into a Seq Scan of the whole child table.

#680 derived the safe-delete guard from the registry — correct, and it made the gap
visible: the three ``summer_league_play_by_play_events.person*_id`` columns had no index,
so deleting one stub Seq-Scanned the fastest-growing table in the schema three times, and
bulk deletion multiplied that by the selection size (#681). Four more registered columns
had the same hole. All seven were indexed in #681; this test is what keeps the next
registration from re-opening it.

The rule
--------
For every ``(table, column)`` in the merge registry, some index on that table must have
*column* as its **leading** column — a plain ``Index``, a ``UniqueConstraint`` (Postgres
backs it with an index), a column-level ``index=True``, or the primary key. A leading
column is what makes the equality lookup an Index Scan; a column buried in position 2 of a
composite does not qualify.

Partial indexes count. All seven columns added in #681 are nullable back-references that
are NULL on most rows, so each is partial on ``col IS NOT NULL`` — and since
``col = :player_id`` is strict, the planner proves the predicate and uses the index anyway.

Deliberately reflective over SQLModel metadata, not the live database: this stays a unit
test, and metadata is the repo's canonical schema definition (migrations follow it).
See ``docs/plans/programmatic-code-discipline.md`` §3.4c.
"""

from __future__ import annotations

import importlib
import pkgutil

from sqlalchemy import Table, UniqueConstraint
from sqlmodel import SQLModel

from app import schemas as schemas_pkg
from app.services.player_merge_service import (
    _CHILD_TABLES,
    _SIMILARITY_ANCHOR,
    _SIMILARITY_COMPARISON,
    _SPEC_TABLE_ALIASES,
)


def _load_all_schemas() -> None:
    """Import every ``app.schemas`` module so SQLModel metadata is complete.

    Mirrors ``alembic/env.py``, inlined rather than imported so this stays a unit test.
    """
    for _, module_name, _ in pkgutil.walk_packages(
        schemas_pkg.__path__, schemas_pkg.__name__ + "."
    ):
        importlib.import_module(module_name)


def _registered_columns() -> list[tuple[str, str]]:
    """Return the ``(table, column)`` pairs the merge service scans, in registry order."""
    specs = (*_CHILD_TABLES, _SIMILARITY_ANCHOR, _SIMILARITY_COMPARISON)
    return [
        (_SPEC_TABLE_ALIASES.get(spec.table, spec.table), spec.player_column)
        for spec in specs
    ]


def _leading_indexes(table: Table, column: str) -> list[str]:
    """Return the names of indexes/constraints on *table* led by *column*."""
    leading: list[str] = []
    for index in table.indexes:
        columns = list(index.columns)
        if columns and columns[0].name == column:
            leading.append(index.name or "<unnamed index>")
    for constraint in table.constraints:
        if not isinstance(constraint, UniqueConstraint):
            continue
        columns = list(constraint.columns)
        if columns and columns[0].name == column:
            leading.append(constraint.name or "<unnamed unique constraint>")
    target = table.columns.get(column)
    if target is not None and target.primary_key:
        leading.append(f"{table.name}_pkey")
    return leading


def test_every_registered_player_column_has_a_leading_index() -> None:
    """A registered child table must be reachable by an Index Scan on its player column.

    Registering the table is what makes the merge and safe-delete paths scan it; without
    a leading index each of those is a full-table Seq Scan, multiplied by the number of
    players a bulk operation touches.
    """
    _load_all_schemas()
    unindexed: list[str] = []
    for table_name, column in _registered_columns():
        table = SQLModel.metadata.tables.get(table_name)
        # A spec naming a table that does not exist is the FK-coverage test's finding,
        # not this one's — skip rather than report the same drift twice.
        if table is None or column not in table.c:
            continue
        if not _leading_indexes(table, column):
            unindexed.append(f"{table_name}.{column}")

    assert not unindexed, (
        "Registered merge columns with no index leading on them:\n"
        + "\n".join(f"  {entry}" for entry in unindexed)
        + "\n\nEvery player merge and every stub deletion Seq-Scans these tables, and so\n"
        "does Postgres's RESTRICT check on the final players_master DELETE.\n"
        "Add an Index to the table's __table_args__ in app/schemas/ plus an Alembic\n"
        "migration (partial on `col IS NOT NULL` if the column is a mostly-NULL\n"
        "back-reference). See docs/plans/programmatic-code-discipline.md §3.4c."
    )


def test_pbp_participant_columns_are_indexed() -> None:
    """Pin the three PBP participant columns specifically — the #681 regression.

    The generic test above would also catch these, but naming them keeps the ticket's
    finding legible: this table is the fastest-growing in the schema and it is counted
    three times per player.
    """
    _load_all_schemas()
    table = SQLModel.metadata.tables["summer_league_play_by_play_events"]
    for column in ("person1_id", "person2_id", "person3_id"):
        assert _leading_indexes(table, column), (
            f"summer_league_play_by_play_events.{column} lost its player-leading index"
        )
