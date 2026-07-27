"""Regression coverage for the production-safe PBP index migration."""

from contextlib import contextmanager
import importlib.util
from pathlib import Path
import sys
from types import ModuleType
from typing import Any, Iterator


MIGRATION_PATH = (
    Path(__file__).parents[2]
    / "alembic/versions/2c78f642217c_add_pbp_competition_game_index.py"
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
    """Minimal Alembic bind used to inspect an existing index's validity."""

    def __init__(self, index_is_valid: bool | None) -> None:
        self.index_is_valid = index_is_valid

    def execute(self, *_args: Any, **_kwargs: Any) -> _FakeScalarResult:
        """Return the configured catalog state."""
        return _FakeScalarResult(self.index_is_valid)


class _FakeOperations:
    """Record migration operations and their transaction context."""

    def __init__(self, *, index_is_valid: bool | None = None) -> None:
        self.context = _FakeMigrationContext()
        self.bind = _FakeBind(index_is_valid)
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any], bool]] = []

    def get_context(self) -> _FakeMigrationContext:
        """Return the context used by the migration under test."""
        return self.context

    def get_bind(self) -> _FakeBind:
        """Return the bind used for the catalog lookup."""
        return self.bind

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
    spec = importlib.util.spec_from_file_location("pbp_index_migration", MIGRATION_PATH)
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


def test_upgrade_builds_index_concurrently_outside_transaction() -> None:
    """Upgrade must not queue an exclusive table lock ahead of public reads."""
    migration = _load_migration()
    fake_op = _FakeOperations()
    migration.op = fake_op

    migration.upgrade()

    assert fake_op.calls == [
        (
            "create_index",
            (
                "ix_summer_league_pbp_events_competition_game",
                "summer_league_play_by_play_events",
                ["competition_id", "game_id"],
            ),
            {"if_not_exists": True, "postgresql_concurrently": True},
            True,
        )
    ]


def test_downgrade_drops_index_concurrently_outside_transaction() -> None:
    """Downgrade should preserve the same non-blocking DDL behavior."""
    migration = _load_migration()
    fake_op = _FakeOperations()
    migration.op = fake_op

    migration.downgrade()

    assert fake_op.calls == [
        (
            "drop_index",
            ("ix_summer_league_pbp_events_competition_game",),
            {
                "table_name": "summer_league_play_by_play_events",
                "if_exists": True,
                "postgresql_concurrently": True,
            },
            True,
        )
    ]


def test_upgrade_replaces_invalid_index_left_by_interrupted_build() -> None:
    """A retry drops an invalid artifact before rebuilding the index."""
    migration = _load_migration()
    fake_op = _FakeOperations(index_is_valid=False)
    migration.op = fake_op

    migration.upgrade()

    assert [call[0] for call in fake_op.calls] == ["drop_index", "create_index"]
    assert fake_op.calls[0][2] == {
        "table_name": "summer_league_play_by_play_events",
        "if_exists": True,
        "postgresql_concurrently": True,
    }
    assert all(call[3] is True for call in fake_op.calls)
