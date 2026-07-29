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

    def test_a_multi_line_waiver_applies(self):
        """The marker may sit anywhere in the contiguous comment block above.

        A one-line lookback rejected the obvious two-line justification, failing with
        the reason sitting right there in the diff. It fails closed, so nothing unsafe
        passed -- but an escape hatch that rejects its own documented shape teaches
        people to disable the hook rather than argue the exception.
        """
        assert (
            _violations(
                """
                def upgrade():
                    # discipline: migration-safety single-row table (one fit in prod);
                    # a non-concurrent build here locks for milliseconds.
                    op.create_index("ix_a", "t", ["a"])
                """
            )
            == []
        )

    def test_a_comment_block_broken_by_code_does_not_reach_back(self):
        """Only the *contiguous* block counts, or a waiver leaks to unrelated statements."""
        found = _violations(
            """
            def upgrade():
                # discipline: migration-safety this justifies the first build only
                op.execute("SELECT 1")
                op.create_index("ix_a", "t", ["a"])
            """
        )
        assert len(found) == 1

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


class TestRawSqlIndexBuilds:
    """`op.execute("CREATE INDEX ...")` takes the same lock as `op.create_index`.

    This spelling is not hypothetical — five existing revisions use it, so it is the
    pattern a new migration is most likely to copy. A checker blind to it would leave
    the house style as the bypass.
    """

    def test_raw_create_index_without_concurrently_is_flagged(self):
        found = _violations(
            """
            def upgrade():
                op.execute(
                    "CREATE INDEX IF NOT EXISTS ix_games_round_label"
                    " ON summer_league_games (round_label)"
                )
            """
        )
        assert len(found) == 1
        assert "raw SQL CREATE INDEX without CONCURRENTLY" in found[0]

    def test_implicitly_concatenated_sql_is_read_as_one_statement(self):
        """The real migrations split this DDL across adjacent literals."""
        found = _violations(
            """
            def upgrade():
                op.execute(
                    "CREATE INDEX IF NOT EXISTS ix_a "
                    "ON players_master USING GIN (display_name gin_trgm_ops)"
                )
            """
        )
        assert len(found) == 1

    def test_raw_unique_index_is_flagged(self):
        found = _violations(
            """
            def upgrade():
                op.execute("CREATE UNIQUE INDEX ix_a ON t (a)")
            """
        )
        assert len(found) == 1

    def test_raw_concurrent_index_outside_block_is_flagged(self):
        found = _violations(
            """
            def upgrade():
                op.execute("CREATE INDEX CONCURRENTLY ix_a ON t (a)")
            """
        )
        assert len(found) == 1
        assert "autocommit_block" in found[0]

    def test_raw_concurrent_index_inside_block_passes(self):
        assert (
            _violations(
                """
                def upgrade():
                    with op.get_context().autocommit_block():
                        op.execute("CREATE INDEX CONCURRENTLY ix_a ON t (a)")
                """
            )
            == []
        )

    def test_sql_wrapped_in_text_is_still_seen(self):
        """``op.execute(text("..."))`` must not slip past the sink detection."""
        found = _violations(
            """
            def upgrade():
                op.execute(text("CREATE INDEX ix_a ON t (a)"))
            """
        )
        assert len(found) == 1

    def test_raw_create_index_can_be_waived_for_a_small_table(self):
        assert (
            _violations(
                """
                def upgrade():
                    # discipline: migration-safety new table, empty at deploy time
                    op.execute("CREATE INDEX ix_widgets ON widgets (name)")
                """
            )
            == []
        )

    def test_prose_about_create_index_is_not_flagged(self):
        """Docstrings explaining this very rule must not trip it.

        `2c78f642217c`'s docstring discusses `CREATE INDEX` precisely because of this
        guard; flagging the documentation of a hazard as the hazard would be
        self-defeating and would teach authors to stop explaining themselves.
        """
        assert (
            _violations(
                '''
                """Concurrent DDL matters: a regular CREATE INDEX queues a lock."""

                def upgrade():
                    with op.get_context().autocommit_block():
                        op.create_index(
                            "ix_a", "t", ["a"], postgresql_concurrently=True
                        )
                '''
            )
            == []
        )

    def test_non_index_sql_is_ignored(self):
        assert (
            _violations(
                """
                def upgrade():
                    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
                    op.execute("UPDATE t SET a = 1")
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

    def test_a_mentioned_but_unexecuted_lock_timeout_is_flagged(self):
        """The regression this rule actually has to catch.

        Deleting the ``execute(set_config(...))`` call while leaving the constant and
        the explanatory comment behind is the realistic way this protection dies — and
        a substring test would wave it straight through.
        """
        found = _violations(
            """
            # A migration waiting forever for a lock can queue ahead of public reads,
            # so bound it with lock_timeout.
            MIGRATION_LOCK_TIMEOUT = os.getenv("ALEMBIC_LOCK_TIMEOUT", "10s")

            async def run_migrations_online():
                async with connectable.connect() as connection:
                    await connection.run_sync(do_run_migrations)
            """,
            name="alembic/env.py",
        )
        assert len(found) == 1
        assert "no executed lock_timeout statement" in found[0]

    def test_env_executing_set_config_passes(self):
        assert (
            _violations(
                """
                MIGRATION_LOCK_TIMEOUT = os.getenv("ALEMBIC_LOCK_TIMEOUT", "10s")

                async def run_migrations_online():
                    await connection.execute(
                        text("SELECT set_config('lock_timeout', :timeout, false)"),
                        {"timeout": MIGRATION_LOCK_TIMEOUT},
                    )
                """,
                name="alembic/env.py",
            )
            == []
        )

    def test_env_executing_a_plain_set_statement_passes(self):
        """The other spelling someone could reasonably reach for."""
        assert (
            _violations(
                """
                async def run_migrations_online():
                    await connection.execute(text("SET lock_timeout = '10s'"))
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


class TestDuplicateRevisionIds:
    """A revision ID claimed twice breaks `alembic upgrade heads` at deploy time.

    Alembic cannot build an unambiguous revision map from two files declaring
    the same ``revision``, so the production ``release_command`` fails outright.
    Nothing else in the repo catches it: the integration fixtures build schemas
    with ``SQLModel.metadata.create_all`` rather than by running migrations, so
    a duplicate passes a fully green test suite and only surfaces on deploy.

    It happens when a revision ID is hand-written rather than generated by
    ``alembic revision`` -- routine for enum-label migrations that need no
    autogenerated body.
    """

    def test_the_live_versions_tree_has_no_duplicates(self):
        """The real check: the migrations actually shipping must be unambiguous."""
        assert checker.duplicate_revision_ids() == []

    def test_a_collision_is_reported_with_both_filenames(self, tmp_path, monkeypatch):
        """The message must name every colliding file, or it isn't actionable."""
        versions = tmp_path / "versions"
        versions.mkdir()
        (versions / "aaa111_first.py").write_text('revision = "aaa111"\n')
        (versions / "bbb222_second.py").write_text('revision = "aaa111"\n')
        (versions / "ccc333_unique.py").write_text('revision = "ccc333"\n')
        monkeypatch.setattr(checker, "_VERSIONS_DIR", versions)

        findings = checker.duplicate_revision_ids()

        assert len(findings) == 1
        assert "aaa111" in findings[0]
        assert "aaa111_first.py" in findings[0]
        assert "bbb222_second.py" in findings[0]
        assert "ccc333" not in findings[0]

    def test_annotated_revision_declarations_are_parsed(self, tmp_path, monkeypatch):
        """Some revisions use `revision: str = "..."`; both spellings must count."""
        versions = tmp_path / "versions"
        versions.mkdir()
        (versions / "aaa111_plain.py").write_text('revision = "shared"\n')
        (versions / "bbb222_typed.py").write_text(
            'revision: str = "shared"\ndown_revision: Union[str, None] = None\n'
        )
        monkeypatch.setattr(checker, "_VERSIONS_DIR", versions)

        findings = checker.duplicate_revision_ids()

        assert len(findings) == 1, "the annotated form must not be silently skipped"
        assert "bbb222_typed.py" in findings[0]
