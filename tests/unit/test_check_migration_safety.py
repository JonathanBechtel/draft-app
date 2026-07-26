"""Tests for the migration-safety discipline checker.

The checker guards against a repeat of incident #669, where a non-concurrent
``CREATE INDEX`` in a release migration queued behind a long ingestion transaction and
took DB-backed public routes down. These tests pin both halves of its contract, because
either half alone is worse than useless: demanding ``CONCURRENTLY`` without demanding the
autocommit block turns a lock hazard into a guaranteed failed release, and demanding the
block without ``CONCURRENTLY`` leaves the original hazard in place.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from tests.unit._script_loader import load_script


checker = load_script("check_migration_safety")


def _violations(source: str, name: str = "abc123_add_index.py") -> list[str]:
    return checker.find_violations(Path(name), dedent(source))


class TestFlagsUnsafeIndexBuilds:
    """The shapes that caused the incident must be reported."""

    def test_bare_create_index_is_flagged(self):
        """The founding failure shape: a non-concurrent index build on a live table."""
        found = _violations(
            """
            def upgrade():
                op.create_index(
                    "ix_pbp_competition_game",
                    "summer_league_play_by_play_events",
                    ["competition_id", "game_id"],
                )
            """
        )
        assert len(found) == 1
        assert "without postgresql_concurrently=True" in found[0]

    def test_explicit_false_is_flagged(self):
        """Passing the keyword as False is the same hazard, spelled out."""
        found = _violations(
            """
            def upgrade():
                op.create_index("ix_a", "t", ["c"], postgresql_concurrently=False)
            """
        )
        assert len(found) == 1
        assert "without postgresql_concurrently=True" in found[0]

    def test_each_offending_call_is_reported_separately(self):
        """Two unsafe builds should surface as two actionable line references."""
        found = _violations(
            """
            def upgrade():
                op.create_index("ix_a", "t", ["a"])
                op.create_index("ix_b", "t", ["b"])
            """
        )
        assert len(found) == 2

    def test_bare_call_without_op_prefix_is_flagged(self):
        """An import alias is not a semantic difference."""
        found = _violations(
            """
            from alembic.op import create_index

            def upgrade():
                create_index("ix_a", "t", ["a"])
            """
        )
        assert len(found) == 1


class TestFlagsConcurrentDdlOutsideAutocommitBlock:
    """The repo-specific half: CONCURRENTLY in a transaction cannot succeed."""

    def test_concurrent_create_outside_block_is_flagged(self):
        """``alembic/env.py`` runs migrations in a transaction; this would fail live."""
        found = _violations(
            """
            def upgrade():
                op.create_index("ix_a", "t", ["a"], postgresql_concurrently=True)
            """
        )
        assert len(found) == 1
        assert "autocommit_block" in found[0]

    def test_concurrent_drop_outside_block_is_flagged(self):
        """Drops are exempt from rule 1 but not from rule 2."""
        found = _violations(
            """
            def downgrade():
                op.drop_index("ix_a", table_name="t", postgresql_concurrently=True)
            """
        )
        assert len(found) == 1
        assert "autocommit_block" in found[0]

    def test_waiver_cannot_suppress_the_broken_release_finding(self):
        """Rule 2 is not a trade-off to argue about — it is a release that fails.

        The small-table escape hatch exists for lock-risk judgement calls. Letting it
        also silence "this DDL cannot run" would hand authors a comment that turns a
        loud local failure into a broken deploy.
        """
        found = _violations(
            """
            def upgrade():
                # discipline: migration-safety tiny table, no lock risk
                op.create_index("ix_a", "t", ["a"], postgresql_concurrently=True)
            """
        )
        assert len(found) == 1
        assert "autocommit_block" in found[0]


class TestAcceptsSafeForms:
    """Correct migrations must stay quiet, or the rule trains people to bypass it."""

    def test_concurrent_build_inside_autocommit_block_passes(self):
        """The shape the repo already uses in ``e7c75f3063ec`` and ``2c78f642217c``."""
        assert (
            _violations(
                """
                def upgrade():
                    with op.get_context().autocommit_block():
                        op.create_index(
                            "ix_a",
                            "t",
                            ["a"],
                            if_not_exists=True,
                            postgresql_concurrently=True,
                        )
                """
            )
            == []
        )

    def test_nested_statements_inside_the_block_still_count_as_inside(self):
        """A conditional drop-then-create inside the block is the recovery pattern."""
        assert (
            _violations(
                """
                def upgrade():
                    with op.get_context().autocommit_block():
                        if index_is_valid is False:
                            op.drop_index(
                                "ix_a", table_name="t", postgresql_concurrently=True
                            )
                        op.create_index(
                            "ix_a", "t", ["a"], postgresql_concurrently=True
                        )
                """
            )
            == []
        )

    def test_non_concurrent_drop_index_is_allowed(self):
        """Dropping an index is brief; forcing an autocommit block around it is noise."""
        assert (
            _violations(
                """
                def downgrade():
                    op.drop_index("ix_a", table_name="t")
                """
            )
            == []
        )

    def test_justified_waiver_exempts_a_small_table(self):
        """New and small tables have no lock problem — with the reason visible."""
        assert (
            _violations(
                """
                def upgrade():
                    # discipline: migration-safety new table, empty at deploy time
                    op.create_index("ix_widgets_name", "widgets", ["name"])
                """
            )
            == []
        )

    def test_bare_marker_without_a_reason_is_not_a_waiver(self):
        """The convention's whole point is that exceptions are argued, not asserted."""
        found = _violations(
            """
            def upgrade():
                # discipline: migration-safety
                op.create_index("ix_widgets_name", "widgets", ["name"])
            """
        )
        assert len(found) == 1

    def test_create_all_migrations_are_untouched(self):
        """Whole-table creation carries its indexes along; there is no DDL to flag."""
        assert (
            _violations(
                """
                def upgrade():
                    SQLModel.metadata.create_all(
                        bind=op.get_bind(), tables=[Widget.__table__]
                    )
                """
            )
            == []
        )


class TestEnvLockTimeout:
    """The third rule: the deploy must fail fast rather than camp in the lock queue."""

    def test_env_without_lock_timeout_is_flagged(self):
        found = _violations(
            """
            async def run_migrations_online():
                async with connectable.connect() as connection:
                    await connection.run_sync(do_run_migrations)
            """,
            name="alembic/env.py",
        )
        assert len(found) == 1
        assert "lock_timeout" in found[0]

    def test_env_with_lock_timeout_passes(self):
        assert (
            _violations(
                """
                MIGRATION_LOCK_TIMEOUT = os.getenv("ALEMBIC_LOCK_TIMEOUT", "10s")
                """,
                name="alembic/env.py",
            )
            == []
        )


class TestAgainstTheRepo:
    """The rule must hold for the code actually shipping, not only for fixtures."""

    def test_live_env_py_configures_a_lock_timeout(self):
        """Regression guard: removing the timeout is what made #669 an outage."""
        env_path = Path(checker.ENV_PATH)
        assert env_path.is_file()
        assert (
            checker.find_violations(
                env_path, env_path.read_text(encoding="utf-8")
            )
            == []
        )

    def test_the_incident_migration_now_passes(self):
        """``2c78f642217c`` is the revision from the incident; it must be clean now."""
        matches = sorted(Path("alembic/versions").glob("2c78f642217c_*.py"))
        assert matches, "expected the incident's revision to exist"
        source = matches[0].read_text(encoding="utf-8")
        assert checker.find_violations(matches[0], source) == []
