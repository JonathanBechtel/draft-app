"""Unit coverage for the Summer League cohort-baseline active guard."""

from __future__ import annotations

from contextlib import contextmanager
import importlib.util
from pathlib import Path
import sys
from types import ModuleType
from typing import Any, Iterator

from app.schemas.summer_league_desk import SummerLeagueCohortBaseline


MIGRATION_PATH = (
    Path(__file__).parents[2]
    / "alembic/versions/8d6f2a1c9b4e_add_cohort_baselines_active_guard.py"
)


class _FakeMigrationContext:
    """Track whether concurrent index DDL runs in an autocommit block."""

    def __init__(self) -> None:
        self.in_autocommit_block = False

    @contextmanager
    def autocommit_block(self) -> Iterator[None]:
        """Mirror Alembic's autocommit context for the migration assertions."""
        self.in_autocommit_block = True
        try:
            yield
        finally:
            self.in_autocommit_block = False


class _FakeScalarResult:
    """Return the configured PostgreSQL catalog validity value."""

    def __init__(self, value: bool | None) -> None:
        self.value = value

    def scalar_one_or_none(self) -> bool | None:
        """Mirror SQLAlchemy's scalar result helper."""
        return self.value


class _FakeBind:
    """Minimal Alembic bind used for the index-validity query."""

    def __init__(self, index_is_valid: bool | None) -> None:
        self.index_is_valid = index_is_valid

    def execute(self, *_args: Any, **_kwargs: Any) -> _FakeScalarResult:
        """Return the configured catalog state."""
        return _FakeScalarResult(self.index_is_valid)


class _FakeOperations:
    """Record migration DDL and whether each operation is outside a transaction."""

    def __init__(self, *, index_is_valid: bool | None) -> None:
        self.context = _FakeMigrationContext()
        self.bind = _FakeBind(index_is_valid)
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any], bool]] = []

    def get_context(self) -> _FakeMigrationContext:
        """Return the fake Alembic migration context."""
        return self.context

    def get_bind(self) -> _FakeBind:
        """Return the fake SQLAlchemy bind."""
        return self.bind

    def execute(self, *args: Any, **kwargs: Any) -> None:
        """Record the deduplication statement."""
        self.calls.append(("execute", args, kwargs, self.context.in_autocommit_block))

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
    """Load the numeric migration module without importing Alembic's runtime."""
    spec = importlib.util.spec_from_file_location(
        "cohort_baselines_guard_migration", MIGRATION_PATH
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


def test_schema_declares_partial_unique_active_index() -> None:
    """The model carries one unique ``cohort_key`` index for active rows only."""
    indexes = {
        index.name: index
        for index in SummerLeagueCohortBaseline.__table__.indexes  # type: ignore[attr-defined]
    }
    active = indexes["uq_summer_league_cohort_baselines_active"]

    assert active.unique is True
    assert list(active.columns.keys()) == ["cohort_key"]
    assert str(active.dialect_options["postgresql"]["where"]) == "is_active"
    assert "ix_summer_league_cohort_baselines_cohort_active" not in indexes


def test_upgrade_drops_invalid_artifact_before_concurrent_rebuild() -> None:
    """A cancelled CIC leaves an invalid index that must be replaced on retry."""
    migration = _load_migration()
    fake_op = _FakeOperations(index_is_valid=False)
    migration.op = fake_op

    migration.upgrade()

    assert [call[0] for call in fake_op.calls] == [
        "execute",
        "drop_index",
        "drop_index",
        "create_index",
    ]
    assert fake_op.calls[0][3] is False
    assert fake_op.calls[1][1] == ("ix_summer_league_cohort_baselines_cohort_active",)
    assert fake_op.calls[2][1] == ("uq_summer_league_cohort_baselines_active",)
    assert fake_op.calls[3][1] == (
        "uq_summer_league_cohort_baselines_active",
        "summer_league_cohort_baselines",
        ["cohort_key"],
    )
    assert fake_op.calls[3][2]["unique"] is True
    assert fake_op.calls[3][2]["postgresql_concurrently"] is True
    assert all(call[3] is True for call in fake_op.calls[1:])


def test_upgrade_is_idempotent_when_unique_index_is_valid() -> None:
    """A fresh ``create_all`` index is reused rather than rebuilt."""
    migration = _load_migration()
    fake_op = _FakeOperations(index_is_valid=True)
    migration.op = fake_op

    migration.upgrade()

    assert [call[0] for call in fake_op.calls] == [
        "execute",
        "drop_index",
        "create_index",
    ]
    assert fake_op.calls[2][2]["if_not_exists"] is True


def test_downgrade_restores_legacy_index_shape() -> None:
    """Downgrade drops the guard and recreates the former advisory index."""
    migration = _load_migration()
    fake_op = _FakeOperations(index_is_valid=True)
    migration.op = fake_op

    migration.downgrade()

    assert [call[0] for call in fake_op.calls] == ["drop_index", "create_index"]
    assert fake_op.calls[0][1] == ("uq_summer_league_cohort_baselines_active",)
    assert fake_op.calls[1][1] == (
        "ix_summer_league_cohort_baselines_cohort_active",
        "summer_league_cohort_baselines",
        ["cohort_key", "is_active"],
    )
    assert fake_op.calls[1][2]["if_not_exists"] is True
