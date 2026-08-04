"""Real-Postgres round-trip coverage for the legacy-snapshot version-stamp migration.

Runs the actual migration module's ``upgrade()`` against a real connection, inside
the standard rollback-isolated ``db_session`` fixture so nothing persists past each
test. This complements the mocked from-scratch-DB guard test in
``tests/unit/test_add_version_stamps_to_legacy_snapshots_migration.py`` with the
thing a mock cannot prove: that the real DDL runs cleanly against real Postgres and
that the sentinel backfill actually lands in the database, not just in a recorded
call list.

``op`` is a small hand-rolled shim, not Alembic's real ``Operations`` facade: this
repo's own ``alembic/`` migrations directory (``alembic/__init__.py``) shadows the
installed ``alembic`` package on ``sys.path`` (position ``''`` / cwd is searched
before site-packages whenever tests run from the repo root -- confirmed no other
module in this codebase imports ``alembic.operations``/``alembic.command`` directly
for exactly this reason). The shim executes ``ALTER TABLE`` statements compiled
from the exact ``sa.Column`` objects the migration file constructs, so it still
exercises the migration's real column definitions (type/nullability/default) and
its idempotency guard (``sa.inspect(op.get_bind())``), just without borrowing
Alembic's internal DDL renderer.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError
from sqlmodel.ext.asyncio.session import AsyncSession

from app.schemas.image_snapshots import IMAGE_PIPELINE_CALCULATION_VERSION
from app.schemas.image_snapshots import LEGACY_VERSION_SENTINEL as IMAGE_SENTINEL
from app.schemas.metrics import LEGACY_VERSION_SENTINEL as METRIC_SENTINEL
from app.schemas.metrics import METRIC_SNAPSHOT_VERSION_TAG

pytestmark = pytest.mark.asyncio

MIGRATION_PATH = (
    Path(__file__).parents[2]
    / "alembic/versions"
    / "88e408e797c6_add_version_stamps_to_legacy_snapshots.py"
)


def _load_migration() -> Any:
    """Load the migration module directly from its numeric filename."""
    spec = importlib.util.spec_from_file_location(
        "add_version_stamps_to_legacy_snapshots_migration_it", MIGRATION_PATH
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


class _RealOperationsShim:
    """Execute ``add_column``/``alter_column``/`drop_column`` for real via SQL text.

    Compiles DDL from the same ``sa.Column`` objects the migration constructs, so
    a change to the migration's column definitions is reflected here automatically
    rather than drifting from a hand-duplicated schema.
    """

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def get_bind(self) -> Connection:
        """Return the bound connection, mirroring Alembic's ``op.get_bind()``."""
        return self._connection

    def add_column(self, table_name: str, column: sa.Column) -> None:
        """Run a real ``ALTER TABLE ... ADD COLUMN`` for ``column``."""
        type_sql = column.type.compile(dialect=self._connection.dialect)
        parts = [f'ALTER TABLE "{table_name}" ADD COLUMN "{column.name}" {type_sql}']
        if column.server_default is not None:
            default_literal = column.server_default.arg  # type: ignore[attr-defined]
            assert isinstance(default_literal, str)
            default_sql = "'" + default_literal.replace("'", "''") + "'"
            parts.append(f"DEFAULT {default_sql}")
        if not column.nullable:
            parts.append("NOT NULL")
        self._connection.execute(text(" ".join(parts)))

    def alter_column(
        self, table_name: str, column_name: str, *, server_default: object = "unset"
    ) -> None:
        """Drop a column's server default -- the only alter this migration does."""
        assert server_default is None, "shim only supports dropping defaults"
        self._connection.execute(
            text(
                f'ALTER TABLE "{table_name}" ALTER COLUMN "{column_name}" DROP DEFAULT'
            )
        )

    def drop_column(self, table_name: str, column_name: str) -> None:
        """Run a real ``ALTER TABLE ... DROP COLUMN``."""
        self._connection.execute(
            text(f'ALTER TABLE "{table_name}" DROP COLUMN "{column_name}"')
        )


async def _run_upgrade(session: AsyncSession) -> None:
    module = _load_migration()
    connection = await session.connection()

    def _apply(sync_conn: Connection) -> None:
        module.op = _RealOperationsShim(sync_conn)
        module.upgrade()

    await connection.run_sync(_apply)


async def test_upgrade_on_current_schema_is_a_noop_and_reads_unchanged(
    db_session: AsyncSession,
) -> None:
    """The from-scratch-DB path: create_all already has the three columns.

    The session-scoped test schema is bootstrapped via
    ``SQLModel.metadata.create_all`` against the *current* model classes (both
    now inherit ``DatedVersionMixin``), so ``metric_snapshots`` and
    ``player_image_snapshots`` already carry ``registry_version`` /
    ``calculation_version`` / ``as_of`` before this migration ever runs. Running
    the real migration against that schema must not error and must not change
    the columns' shape -- and existing read paths (a plain SELECT of
    version/is_current) must return the same values as before.
    """
    await db_session.execute(
        text(
            "INSERT INTO metric_snapshots "
            "(run_key, cohort, source, population_size, calculated_at, version, "
            " is_current, registry_version, calculation_version) "
            "VALUES ('it-noop-run', 'current_draft', 'combine_anthro', 5, now(), "
            " 1, true, :registry, :calc)"
        ),
        {"registry": METRIC_SNAPSHOT_VERSION_TAG, "calc": METRIC_SNAPSHOT_VERSION_TAG},
    )

    await _run_upgrade(db_session)

    row = (
        await db_session.execute(
            text(
                "SELECT version, is_current, registry_version, calculation_version "
                "FROM metric_snapshots WHERE run_key = 'it-noop-run'"
            )
        )
    ).one()
    assert row.version == 1
    assert row.is_current is True
    assert row.registry_version == METRIC_SNAPSHOT_VERSION_TAG
    assert row.calculation_version == METRIC_SNAPSHOT_VERSION_TAG

    columns = (
        (
            await db_session.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'metric_snapshots'"
                )
            )
        )
        .scalars()
        .all()
    )
    assert {"registry_version", "calculation_version", "as_of"} <= set(columns)


async def test_upgrade_backfills_pre_migration_rows_with_sentinel(
    db_session: AsyncSession,
) -> None:
    """A database on the previous head backfills existing rows honestly.

    Reverts ``metric_snapshots``/``player_image_snapshots`` to their pre-#785
    shape (within this test's rolled-back transaction only), inserts rows the
    way the old schema would have held them, runs the real migration, and
    asserts: (1) the pre-existing rows land on the documented sentinel, never a
    fabricated version, and (2) a row written *after* the migration with
    explicit real values is not coerced back to the sentinel.
    """
    for table in ("metric_snapshots", "player_image_snapshots"):
        await db_session.execute(
            text(
                f"ALTER TABLE {table} "
                "DROP COLUMN registry_version, "
                "DROP COLUMN calculation_version, "
                "DROP COLUMN as_of"
            )
        )

    await db_session.execute(
        text(
            "INSERT INTO metric_snapshots "
            "(run_key, cohort, source, population_size, calculated_at, version, "
            " is_current) "
            "VALUES ('it-legacy-run', 'current_draft', 'combine_anthro', 5, now(), "
            " 1, false)"
        )
    )
    await db_session.execute(
        text(
            "INSERT INTO player_image_snapshots "
            "(run_key, version, is_current, style, cohort, image_size, "
            " system_prompt, generated_at, population_size, success_count, "
            " failure_count) "
            "VALUES ('it-legacy-images', 1, false, 'default', 'global_scope', "
            " '1K', 'legacy prompt', now(), 1, 1, 0)"
        )
    )

    await _run_upgrade(db_session)

    legacy_metric_row = (
        await db_session.execute(
            text(
                "SELECT registry_version, calculation_version, as_of "
                "FROM metric_snapshots WHERE run_key = 'it-legacy-run'"
            )
        )
    ).one()
    assert legacy_metric_row.registry_version == METRIC_SENTINEL
    assert legacy_metric_row.calculation_version == METRIC_SENTINEL
    assert legacy_metric_row.as_of is None

    legacy_image_row = (
        await db_session.execute(
            text(
                "SELECT registry_version, calculation_version, as_of "
                "FROM player_image_snapshots WHERE run_key = 'it-legacy-images'"
            )
        )
    ).one()
    assert legacy_image_row.registry_version == IMAGE_SENTINEL
    assert legacy_image_row.calculation_version == IMAGE_SENTINEL
    assert legacy_image_row.as_of is None

    # The server default is dropped after backfill: an insert that forgets the
    # now-required columns must fail loudly rather than silently land on the
    # sentinel again.
    # Specifically a NOT NULL violation -- a bare ``Exception`` would also be
    # satisfied by a typo'd table or column name, which would say nothing about
    # the migration.
    with pytest.raises(IntegrityError, match="null value in column"):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "INSERT INTO metric_snapshots "
                    "(run_key, cohort, source, population_size, calculated_at, "
                    " version, is_current) "
                    "VALUES ('it-missing-versions', 'current_draft', "
                    "'combine_anthro', 1, now(), 1, false)"
                )
            )

    # A publisher writing after the migration stamps real values, not the sentinel.
    await db_session.execute(
        text(
            "INSERT INTO metric_snapshots "
            "(run_key, cohort, source, population_size, calculated_at, version, "
            " is_current, registry_version, calculation_version) "
            "VALUES ('it-real-run', 'current_draft', 'combine_anthro', 5, now(), "
            " 1, true, :registry, :calc)"
        ),
        {"registry": METRIC_SNAPSHOT_VERSION_TAG, "calc": METRIC_SNAPSHOT_VERSION_TAG},
    )
    real_metric_row = (
        await db_session.execute(
            text(
                "SELECT registry_version, calculation_version "
                "FROM metric_snapshots WHERE run_key = 'it-real-run'"
            )
        )
    ).one()
    assert real_metric_row.registry_version == METRIC_SNAPSHOT_VERSION_TAG
    assert real_metric_row.registry_version != METRIC_SENTINEL
    assert real_metric_row.calculation_version == METRIC_SNAPSHOT_VERSION_TAG

    await db_session.execute(
        text(
            "INSERT INTO player_image_snapshots "
            "(run_key, version, is_current, style, cohort, image_size, "
            " system_prompt, generated_at, population_size, success_count, "
            " failure_count, registry_version, calculation_version) "
            "VALUES ('it-real-images', 1, true, 'default', 'global_scope', "
            " '1K', 'real prompt', now(), 1, 1, 0, :registry, :calc)"
        ),
        {"registry": "default", "calc": IMAGE_PIPELINE_CALCULATION_VERSION},
    )
    real_image_row = (
        await db_session.execute(
            text(
                "SELECT registry_version, calculation_version "
                "FROM player_image_snapshots WHERE run_key = 'it-real-images'"
            )
        )
    ).one()
    assert real_image_row.registry_version == "default"
    assert real_image_row.calculation_version == IMAGE_PIPELINE_CALCULATION_VERSION
    assert real_image_row.calculation_version != IMAGE_SENTINEL
