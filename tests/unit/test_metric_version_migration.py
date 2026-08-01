"""Regression coverage for Summer League metric-version index recovery."""

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
    / "alembic/versions/c5d6e7f8a9b0_version_summer_league_metrics.py"
)


class _FakeMigrationContext:
    """Track whether index DDL runs outside Alembic's transaction."""

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
    """Return a configured catalog value."""

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
    """Expose the existing unique constraint and current index to the migration."""

    def get_columns(self, _table: str) -> list[dict[str, str]]:
        """Return enough metadata for the migration's idempotence checks."""
        return [{"name": "competition_id"}]

    def get_unique_constraints(self, _table: str) -> list[dict[str, str]]:
        """Pretend the versioned unique constraint already exists."""
        return [{"name": "uq_summer_league_metric_contexts_competition_version"}]

    def get_indexes(self, _table: str) -> list[dict[str, str]]:
        """Pretend the current-pointer index exists in the catalog."""
        return [{"name": "uq_summer_league_metric_contexts_current"}]


class _FakeOperations:
    """Record the recovery DDL and its transaction context."""

    def __init__(self, *, index_is_valid: bool | None) -> None:
        self.context = _FakeMigrationContext()
        self.bind = _FakeBind(index_is_valid)
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any], bool]] = []

    def get_context(self) -> _FakeMigrationContext:
        """Return the context used by the migration under test."""
        return self.context

    def get_bind(self) -> _FakeBind:
        """Return the bind used by the catalog lookup."""
        return self.bind

    def drop_index(self, *args: Any, **kwargs: Any) -> None:
        """Record a drop-index operation."""
        self.calls.append(
            ("drop_index", args, kwargs, self.context.in_autocommit_block)
        )

    def create_index(self, *args: Any, **kwargs: Any) -> None:
        """Record a create-index operation."""
        self.calls.append(
            ("create_index", args, kwargs, self.context.in_autocommit_block)
        )


def _load_migration() -> Any:
    """Load the migration module directly from its numeric filename."""
    spec = importlib.util.spec_from_file_location(
        "metric_version_migration", MIGRATION_PATH
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


def test_invalid_current_index_is_replaced_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed prior build must not be mistaken for an enforced unique index."""
    migration = _load_migration()
    fake_op = _FakeOperations(index_is_valid=False)
    migration.op = fake_op
    monkeypatch.setattr(migration.sa, "inspect", lambda _bind: _FakeInspector())

    migration._create_constraints(
        "summer_league_metric_contexts",
        migration._TABLES["summer_league_metric_contexts"],
    )

    assert [call[0] for call in fake_op.calls] == ["drop_index", "create_index"]
    assert fake_op.calls[0][2] == {
        "table_name": "summer_league_metric_contexts",
        "if_exists": True,
        "postgresql_concurrently": True,
    }
    assert fake_op.calls[1][2]["if_not_exists"] is True
    assert fake_op.calls[1][2]["postgresql_concurrently"] is True
    assert all(call[3] is True for call in fake_op.calls)


def test_valid_current_index_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid existing index must not be rebuilt on every migration retry."""
    migration = _load_migration()
    fake_op = _FakeOperations(index_is_valid=True)
    migration.op = fake_op
    monkeypatch.setattr(migration.sa, "inspect", lambda _bind: _FakeInspector())

    migration._create_constraints(
        "summer_league_metric_contexts",
        migration._TABLES["summer_league_metric_contexts"],
    )

    assert fake_op.calls == []
