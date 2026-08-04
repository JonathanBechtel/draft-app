"""Regression coverage for the team_program_id-on-sl-team-entries migration.

Pins the from-scratch-DB guard this repo has been bitten by before: a table
created via live-class ``SQLModel.metadata.create_all`` on a fresh database
already carries every column/index the current model class defines, so a
subsequent ``add_column``/``create_index`` migration must no-op rather than
error. Mocks ``op``/``sa.inspect`` the same way
``tests/unit/test_add_team_program_id_migration.py`` (T4, #783) does, so this
stays a fast unit test while still exercising the migration module's real
guard logic.
"""

from __future__ import annotations

from contextlib import contextmanager
import importlib.util
from pathlib import Path
import sys
from types import ModuleType
from typing import Any, Iterator

import pytest


MIGRATION_PATH = (
    Path(__file__).parents[2]
    / "alembic/versions"
    / "f8855e75c831_add_team_program_id_to_sl_team_entries.py"
)


class _FakeMigrationContext:
    """Track whether index DDL runs outside Alembic's migration transaction."""

    def __init__(self) -> None:
        self.in_autocommit_block = False

    @contextmanager
    def autocommit_block(self) -> Iterator[None]:
        """Mirror Alembic's autocommit block for a focused unit assertion."""
        self.in_autocommit_block = True
        try:
            yield
        finally:
            self.in_autocommit_block = False


class _FakeScalarResult:
    """Return a configured scalar from the migration's catalog query."""

    def __init__(self, value: bool | None) -> None:
        self.value = value

    def scalar_one_or_none(self) -> bool | None:
        """Mirror SQLAlchemy's scalar-result helper."""
        return self.value


class _FakeBind:
    """Minimal Alembic bind used for the index-validity lookup."""

    def __init__(self, index_is_valid: bool | None) -> None:
        self.index_is_valid = index_is_valid

    def execute(self, *_args: Any, **_kwargs: Any) -> _FakeScalarResult:
        """Return the configured catalog state."""
        return _FakeScalarResult(self.index_is_valid)


class _FakeInspector:
    """Report a configurable catalog state for the guarded column/index."""

    def __init__(
        self, *, column_exists: bool, existing_index_names: frozenset[str]
    ) -> None:
        self._column_exists = column_exists
        self._existing_index_names = existing_index_names

    def get_columns(self, _table: str) -> list[dict[str, str]]:
        """Return the catalog columns, including team_program_id if configured."""
        columns = [{"name": "id"}, {"name": "nba_team_id"}]
        if self._column_exists:
            columns.append({"name": "team_program_id"})
        return columns

    def get_indexes(self, _table: str) -> list[dict[str, str]]:
        """Return the catalog indexes already present."""
        return [{"name": name} for name in self._existing_index_names]


class _FakeOperations:
    """Record migration DDL calls and their transaction context."""

    def __init__(self, *, index_is_valid: bool | None = None) -> None:
        self.context = _FakeMigrationContext()
        self.bind = _FakeBind(index_is_valid)
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any], bool]] = []

    def get_context(self) -> _FakeMigrationContext:
        """Return the context used by the migration under test."""
        return self.context

    def get_bind(self) -> _FakeBind:
        """Return the bind used for catalog lookups."""
        return self.bind

    def execute(self, *args: Any, **kwargs: Any) -> None:
        """Record a raw-SQL execute (the legacy-FK convergence drop)."""
        self.calls.append(("execute", args, kwargs, False))

    def add_column(self, *args: Any, **kwargs: Any) -> None:
        """Record an add-column operation."""
        self.calls.append(("add_column", args, kwargs, False))

    def drop_column(self, *args: Any, **kwargs: Any) -> None:
        """Record a drop-column operation."""
        self.calls.append(("drop_column", args, kwargs, False))

    def create_index(self, *args: Any, **kwargs: Any) -> None:
        """Record a create-index operation."""
        self.calls.append(
            ("create_index", args, kwargs, self.context.in_autocommit_block)
        )

    def drop_index(self, *args: Any, **kwargs: Any) -> None:
        """Record a drop-index operation."""
        self.calls.append(
            ("drop_index", args, kwargs, self.context.in_autocommit_block)
        )


def _load_migration() -> Any:
    """Load the migration module directly from its numeric filename."""
    spec = importlib.util.spec_from_file_location(
        "add_team_program_id_to_sl_team_entries_migration", MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    alembic_stub = ModuleType("alembic")
    setattr(alembic_stub, "op", object())
    existing_alembic = sys.modules.get("alembic")
    sys.modules["alembic"] = alembic_stub
    try:
        spec.loader.exec_module(module)
    finally:
        if existing_alembic is None:
            del sys.modules["alembic"]
        else:
            sys.modules["alembic"] = existing_alembic
    return module


def _patch_inspect(
    monkeypatch: pytest.MonkeyPatch,
    migration: Any,
    *,
    column_exists: bool,
    existing_index_names: frozenset[str] = frozenset(),
) -> None:
    monkeypatch.setattr(
        migration.sa,
        "inspect",
        lambda _bind: _FakeInspector(
            column_exists=column_exists, existing_index_names=existing_index_names
        ),
    )


def test_upgrade_on_normal_head_adds_column_and_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A database on the previous head gets the column and index exactly once."""
    migration = _load_migration()
    fake_op = _FakeOperations()
    migration.op = fake_op
    _patch_inspect(monkeypatch, migration, column_exists=False)

    migration.upgrade()

    call_names = [call[0] for call in fake_op.calls]
    assert call_names == ["execute", "add_column", "create_index"]
    # The legacy-FK convergence drop is idempotent and names the old constraint.
    drop_sql = fake_op.calls[0][1][0]
    assert "DROP CONSTRAINT IF EXISTS" in drop_sql
    assert "summer_league_team_entries_team_program_id_fkey" in drop_sql
    add_column_args = fake_op.calls[1][1]
    assert add_column_args[0] == "summer_league_team_entries"
    assert add_column_args[1].name == "team_program_id"
    # Soft reference: no DB-level FK, so upgrade-from-base does not
    # forward-reference team_programs via the earlier create_all() migration.
    assert add_column_args[1].foreign_keys == set()
    # The index build ran inside the autocommit block, concurrently.
    index_call = fake_op.calls[2]
    assert index_call[2]["postgresql_concurrently"] is True
    assert index_call[3] is True


def test_upgrade_on_fresh_create_all_database_is_a_no_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The from-scratch-DB path: create_all already reflects the updated model.

    A fresh database bootstrapped via ``SQLModel.metadata.create_all`` against
    the *current* model class already has ``team_program_id`` and its index
    by the time this migration runs -- it must not attempt to add them again.
    """
    migration = _load_migration()
    fake_op = _FakeOperations()
    migration.op = fake_op
    _patch_inspect(
        monkeypatch,
        migration,
        column_exists=True,
        existing_index_names=frozenset({migration._INDEX_NAME}),
    )

    migration.upgrade()

    # Only the idempotent legacy-FK convergence drop runs; no DDL is re-applied.
    assert [call[0] for call in fake_op.calls] == ["execute"]


def test_upgrade_replaces_invalid_index_left_by_interrupted_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry after a cancelled CONCURRENTLY build drops the INVALID entry first."""
    migration = _load_migration()
    fake_op = _FakeOperations(index_is_valid=False)
    migration.op = fake_op
    _patch_inspect(monkeypatch, migration, column_exists=True)

    migration.upgrade()

    call_names = [call[0] for call in fake_op.calls]
    assert call_names == ["execute", "drop_index", "create_index"]
    # Both index operations run inside the autocommit block, as CONCURRENTLY
    # requires. The legacy-FK convergence drop is ordinary transactional DDL.
    assert all(
        call[3] is True for call in fake_op.calls if call[0].endswith("_index")
    )


def test_downgrade_drops_index_then_column(monkeypatch: pytest.MonkeyPatch) -> None:
    """Downgrade removes the index and the column, guarded the same way."""
    migration = _load_migration()
    fake_op = _FakeOperations()
    migration.op = fake_op
    _patch_inspect(monkeypatch, migration, column_exists=True)

    migration.downgrade()

    call_names = [call[0] for call in fake_op.calls]
    assert call_names == ["drop_index", "drop_column"]
    assert fake_op.calls[0][3] is True


def test_downgrade_on_already_clean_database_is_a_no_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second downgrade (or a DB that never had the column) drops nothing."""
    migration = _load_migration()
    fake_op = _FakeOperations()
    migration.op = fake_op
    _patch_inspect(monkeypatch, migration, column_exists=False)

    migration.downgrade()

    call_names = [call[0] for call in fake_op.calls]
    assert call_names == ["drop_index"]
