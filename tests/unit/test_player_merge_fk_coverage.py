"""Reflective coverage test: every FK to ``players_master`` must be classified.

Failure this descends from
--------------------------
``player_merge_service``'s child-table list is maintained by hand. It was never updated as
``summer_league_*``, shot-event and participation tables added foreign keys to
``players_master``, so merging a player who holds Summer League data hard-fails on a RESTRICT
foreign key — during a merge, which is exactly when nobody has time to debug it. Nothing made
the drift visible. See ``docs/plans/summer-league-simplification-backlog.md`` 4.4 and
``docs/plans/programmatic-code-discipline.md`` §3.4.

The rule
--------
The FK graph supplies the *audit universe*; a human classifies each edge once; this test
enforces that the classification stays total. Every column with a foreign key to
``players_master`` must fall into one of three classes:

``reassign``
    Registered with the merge service, which repoints the row at the survivor.
``cascade``
    Declared ``ondelete="CASCADE"`` — rows that intentionally die with the discarded
    identity (``player_embeddings``, ``pending_image_previews``,
    ``summer_league_player_seasons`` — the last a regenerable metrics projection).
``null-out``
    A nullable back-reference the merge service blanks rather than repoints. Spelled with a
    sentinel spec name that ``_merge_child_table`` special-cases; see ``_SENTINEL_TABLES``.

Deliberately **not** auto-derived: reflective reassignment would resurrect rows the cascade
semantics intend to delete. Classification is a human decision recorded in code.

The baseline — now empty
------------------------
This guard shipped with thirteen unclassified columns enumerated in
``_KNOWN_UNCLASSIFIED``: a ratchet in the same shape as the repo's other guardrail baselines
(Ruff ``per-file-ignores``, import-linter ``ignore_imports``), where a **new** FK fails
immediately and the list can only shrink.

**All thirteen have since been classified** (#675) — registered for reassignment in
``player_merge_service``, so a merge no longer RESTRICT-fails on Summer League data. The
baseline is kept as an empty frozenset rather than deleted, so the next FK that arrives
unclassified fails against zero tolerance instead of quietly re-establishing a list.
"""

from __future__ import annotations

import importlib
import pkgutil

from sqlmodel import SQLModel

from app import schemas as schemas_pkg
from app.services.player_merge_service import (
    _CHILD_TABLES,
    _SIMILARITY_ANCHOR,
    _SIMILARITY_COMPARISON,
)


PARENT_TABLE = "players_master"

# Spec names that are not real tables: ``_merge_child_table`` matches on them and runs
# hand-written SQL against the real table/column below. Mapping them here keeps the
# reflective walk honest about what is genuinely covered.
_SENTINEL_TABLES: dict[str, tuple[str, str]] = {
    "source_analytics_outlier": ("source_analytics", "biggest_outlier_player_id"),
}

# Columns with an FK to players_master that are neither registered for reassignment nor
# declared CASCADE. Empty since #675 classified the original thirteen, and it stays that way:
# the stale-entry test below means an entry added here must be genuinely unclassified, and
# the ratchet only ever shrinks.
_KNOWN_UNCLASSIFIED: frozenset[tuple[str, str]] = frozenset()


def _load_all_schemas() -> None:
    """Import every ``app.schemas`` module so SQLModel metadata is complete.

    Mirrors ``alembic/env.py`` and ``app.utils.db_async``, inlined rather than imported so
    this stays a unit test: importing ``db_async`` would pull in settings and engine setup.
    """
    for _, module_name, _ in pkgutil.walk_packages(
        schemas_pkg.__path__, schemas_pkg.__name__ + "."
    ):
        importlib.import_module(module_name)


def _fk_columns_to_players_master() -> set[tuple[str, str]]:
    """Return every ``(table, column)`` holding a foreign key into ``players_master``."""
    _load_all_schemas()
    found: set[tuple[str, str]] = set()
    for table in SQLModel.metadata.tables.values():
        for column in table.columns:
            for fk in column.foreign_keys:
                if fk.column.table.name == PARENT_TABLE:
                    found.add((table.name, column.name))
    return found


def _cascade_columns() -> set[tuple[str, str]]:
    """Return the ``(table, column)`` pairs whose FK to ``players_master`` cascades."""
    _load_all_schemas()
    found: set[tuple[str, str]] = set()
    for table in SQLModel.metadata.tables.values():
        for column in table.columns:
            for fk in column.foreign_keys:
                if fk.column.table.name != PARENT_TABLE:
                    continue
                if (fk.ondelete or "").upper() == "CASCADE":
                    found.add((table.name, column.name))
    return found


def _registered_columns() -> set[tuple[str, str]]:
    """Return the ``(table, column)`` pairs the merge service reassigns or blanks."""
    specs = (*_CHILD_TABLES, _SIMILARITY_ANCHOR, _SIMILARITY_COMPARISON)
    return {
        _SENTINEL_TABLES.get(spec.table, (spec.table, spec.player_column))
        for spec in specs
    }


def test_every_fk_to_players_master_is_classified() -> None:
    """A new FK to players_master must be registered or cascade — never silently added.

    This is the guard that 4.4's drift recurs behind. Adding a table with an
    unclassified FK fails here rather than during a time-sensitive production merge.
    """
    unclassified = (
        _fk_columns_to_players_master() - _registered_columns() - _cascade_columns()
    )
    surprises = sorted(unclassified - _KNOWN_UNCLASSIFIED)

    assert not surprises, (
        "Unclassified foreign key(s) to players_master:\n"
        + "\n".join(f"  {table}.{column}" for table, column in surprises)
        + "\n\nA merge will RESTRICT-fail if the discarded player holds rows here.\n"
        "Classify each one:\n"
        "  * reassign — add a _ChildTable spec in app/services/player_merge_service.py\n"
        '  * cascade  — declare ondelete="CASCADE" if the rows should die with the\n'
        "               discarded identity (needs a migration)\n"
        "See docs/plans/programmatic-code-discipline.md §3.4."
    )


def test_unclassified_baseline_has_no_stale_entries() -> None:
    """The baseline is a ratchet: classify a column and its entry must be removed.

    Without this, a fixed entry lingers and the list stops measuring anything.
    """
    unclassified = (
        _fk_columns_to_players_master() - _registered_columns() - _cascade_columns()
    )
    stale = sorted(_KNOWN_UNCLASSIFIED - unclassified)

    assert not stale, (
        "These columns are now classified but still listed in _KNOWN_UNCLASSIFIED:\n"
        + "\n".join(f"  {table}.{column}" for table, column in stale)
        + "\n\nRemove them from the baseline — it may only shrink."
    )


def test_registered_specs_point_at_real_columns() -> None:
    """The inverse drift: a spec naming a table or column that no longer exists.

    ``_merge_child_table`` interpolates these names straight into SQL, so a stale spec is
    an error raised mid-merge rather than a caught typo.
    """
    _load_all_schemas()
    missing: list[str] = []
    for table_name, column_name in sorted(_registered_columns()):
        table = SQLModel.metadata.tables.get(table_name)
        if table is None:
            missing.append(f"{table_name} (no such table)")
        elif column_name not in table.c:
            missing.append(f"{table_name}.{column_name} (no such column)")

    assert not missing, "Merge specs reference columns that do not exist:\n" + "\n".join(
        f"  {entry}" for entry in missing
    )
