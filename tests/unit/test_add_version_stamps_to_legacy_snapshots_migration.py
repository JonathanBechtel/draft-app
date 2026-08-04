"""Regression coverage for the legacy-snapshot version-stamp migration.

Pins the from-scratch-DB guard this repo has been bitten by before: a table
created via live-class ``SQLModel.metadata.create_all`` on a fresh database
already carries every column the current model class defines (``metric_snapshots``
/ ``player_image_snapshots`` now inherit ``DatedVersionMixin``), so a subsequent
``add_column`` migration must no-op rather than error. Mocks ``op``/``sa.inspect``
the same way ``tests/unit/test_add_team_program_to_sl_team_entries_migration.py``
(#784) does, so this stays a fast unit test while still exercising the
migration module's real guard logic.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

MIGRATION_PATH = (
    Path(__file__).parents[2]
    / "alembic/versions"
    / "88e408e797c6_add_version_stamps_to_legacy_snapshots.py"
)

_ALL_TABLES = ("metric_snapshots", "player_image_snapshots")


class _FakeInspector:
    """Report a configurable catalog state for the guarded columns."""

    def __init__(self, *, existing_columns: dict[str, frozenset[str]]) -> None:
        self._existing_columns = existing_columns

    def get_columns(self, table_name: str) -> list[dict[str, str]]:
        """Return the catalog columns already present on ``table_name``."""
        base = {"id", "version", "is_current"}
        base |= self._existing_columns.get(table_name, frozenset())
        return [{"name": name} for name in base]


class _FakeOperations:
    """Record migration DDL calls without touching a real database."""

    def __init__(self) -> None:
        self.bind = object()
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def get_bind(self) -> object:
        """Return the bind used for catalog lookups."""
        return self.bind

    def add_column(self, *args: Any, **kwargs: Any) -> None:
        """Record an add-column operation."""
        self.calls.append(("add_column", args, kwargs))

    def alter_column(self, *args: Any, **kwargs: Any) -> None:
        """Record an alter-column operation."""
        self.calls.append(("alter_column", args, kwargs))

    def drop_column(self, *args: Any, **kwargs: Any) -> None:
        """Record a drop-column operation."""
        self.calls.append(("drop_column", args, kwargs))


def _load_migration() -> Any:
    """Load the migration module directly from its numeric filename."""
    spec = importlib.util.spec_from_file_location(
        "add_version_stamps_to_legacy_snapshots_migration", MIGRATION_PATH
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
    existing_columns: dict[str, frozenset[str]] | None = None,
) -> None:
    monkeypatch.setattr(
        migration.sa,
        "inspect",
        lambda _bind: _FakeInspector(existing_columns=existing_columns or {}),
    )


def test_upgrade_on_normal_head_adds_all_columns_with_sentinel_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A database on the previous head gets all three columns, on both tables."""
    migration = _load_migration()
    fake_op = _FakeOperations()
    migration.op = fake_op
    _patch_inspect(monkeypatch, migration)

    migration.upgrade()

    call_names = [call[0] for call in fake_op.calls]
    # Per table: add registry_version, alter (drop default), add
    # calculation_version, alter (drop default), add as_of.
    assert call_names == [
        "add_column",
        "alter_column",
        "add_column",
        "alter_column",
        "add_column",
    ] * len(_ALL_TABLES)

    # First table's registry_version add carries the sentinel server default.
    first_add = fake_op.calls[0]
    assert first_add[1][0] == "metric_snapshots"
    assert first_add[1][1].name == "registry_version"
    assert first_add[1][1].nullable is False
    assert first_add[1][1].server_default is not None

    # Its paired alter_column drops that default.
    first_alter = fake_op.calls[1]
    assert first_alter[1] == ("metric_snapshots", "registry_version")
    assert first_alter[2]["server_default"] is None

    # as_of lands nullable, with no server default.
    as_of_add = fake_op.calls[4]
    assert as_of_add[1][0] == "metric_snapshots"
    assert as_of_add[1][1].name == "as_of"
    assert as_of_add[1][1].nullable is True

    # Second table (player_image_snapshots) mirrors the same shape.
    second_table_add = fake_op.calls[5]
    assert second_table_add[1][0] == "player_image_snapshots"
    assert second_table_add[1][1].name == "registry_version"


def test_upgrade_on_fresh_create_all_database_is_a_no_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The from-scratch-DB path: create_all already reflects the updated model.

    A fresh database bootstrapped via ``SQLModel.metadata.create_all`` against
    the *current* model classes (both now inherit ``DatedVersionMixin``)
    already has ``registry_version`` / ``calculation_version`` / ``as_of`` by
    the time this migration runs -- it must not attempt to add them again.
    """
    migration = _load_migration()
    fake_op = _FakeOperations()
    migration.op = fake_op
    _patch_inspect(
        monkeypatch,
        migration,
        existing_columns={
            table: frozenset({"registry_version", "calculation_version", "as_of"})
            for table in _ALL_TABLES
        },
    )

    migration.upgrade()

    assert fake_op.calls == []


def test_upgrade_only_backfills_missing_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A partially-migrated database (e.g. an interrupted prior run) tops up."""
    migration = _load_migration()
    fake_op = _FakeOperations()
    migration.op = fake_op
    _patch_inspect(
        monkeypatch,
        migration,
        existing_columns={"metric_snapshots": frozenset({"registry_version"})},
    )

    migration.upgrade()

    metric_snapshot_calls = [
        call for call in fake_op.calls if call[1][0] == "metric_snapshots"
    ]
    added_columns = {
        call[1][1].name for call in metric_snapshot_calls if call[0] == "add_column"
    }
    assert added_columns == {"calculation_version", "as_of"}

    image_snapshot_calls = [
        call for call in fake_op.calls if call[1][0] == "player_image_snapshots"
    ]
    added_image_columns = {
        call[1][1].name for call in image_snapshot_calls if call[0] == "add_column"
    }
    assert added_image_columns == {"registry_version", "calculation_version", "as_of"}


def test_downgrade_drops_all_three_columns_per_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Downgrade removes every column it can find, guarded the same way."""
    migration = _load_migration()
    fake_op = _FakeOperations()
    migration.op = fake_op
    _patch_inspect(
        monkeypatch,
        migration,
        existing_columns={
            table: frozenset({"registry_version", "calculation_version", "as_of"})
            for table in _ALL_TABLES
        },
    )

    migration.downgrade()

    dropped = [
        (call[1][0], call[1][1]) for call in fake_op.calls if call[0] == "drop_column"
    ]
    assert set(dropped) == {
        (table, column)
        for table in _ALL_TABLES
        for column in ("registry_version", "calculation_version", "as_of")
    }


def test_downgrade_on_already_clean_database_is_a_no_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second downgrade (or a DB that never had the columns) drops nothing."""
    migration = _load_migration()
    fake_op = _FakeOperations()
    migration.op = fake_op
    _patch_inspect(monkeypatch, migration)

    migration.downgrade()

    assert fake_op.calls == []
