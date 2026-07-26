"""Tests for the `app/cli` vs `scripts/` runtime-entrypoint boundary guard.

The rule this closes: the split between shipped runtime jobs (`app/cli/`) and operator
tooling (`scripts/`) is invisible to every other check in the repo. A Fly cron toml can
be pointed at a `scripts/` path, or a runtime job can grow an `import scripts.foo`, and
nothing fails — the Dockerfile's `COPY . .` ships `scripts/` anyway, so production keeps
working right up until someone tightens `.dockerignore`. These tests pin both halves.
"""

from __future__ import annotations

import ast

from tests.unit._script_loader import load_script


guard = load_script("check_runtime_entrypoints")


class TestCronEntrypoints:
    """E1, Fly toml half: a live `cron =` assignment may not reference scripts/."""

    def test_cron_pointing_at_scripts_is_a_violation(self):
        """The exact shape that shipped for months before the boundary was drawn."""
        assert guard.cron_line_violates(
            '  cron = "/app/.venv/bin/python scripts/sl_desk_tick.py"'
        )

    def test_module_form_entrypoint_is_clean(self):
        """The required form: runtime jobs are invoked as `-m app.cli.<module>`."""
        assert not guard.cron_line_violates(
            '  cron = "/app/.venv/bin/python -m app.cli.sl_desk_tick"'
        )

    def test_commented_out_cron_is_not_a_violation(self):
        """Several tomls quote the historical `flyctl machine run` command in comments.

        Documentation of how a machine was created is not a live entrypoint; flagging it
        would make the guard unfixable without deleting the docs.
        """
        assert not guard.cron_line_violates(
            '#  cron = "/app/.venv/bin/python scripts/sl_desk_tick.py"'
        )

    def test_unrelated_key_mentioning_scripts_is_not_a_violation(self):
        """Only the `cron` key is an entrypoint; other keys may mention paths freely."""
        assert not guard.cron_line_violates('  description = "replaces scripts/foo.py"')


class TestWorkflowEntrypoints:
    """E1, workflow half: the `--` command tail of a `flyctl machine run`."""

    def test_double_dash_tail_pointing_at_scripts_is_a_violation(self):
        assert guard.workflow_line_violates("              -- scripts/sl_desk_tick.py")

    def test_module_form_tail_is_clean(self):
        assert not guard.workflow_line_violates("              -- -m app.cli.sl_desk_tick")

    def test_commented_tail_is_not_a_violation(self):
        assert not guard.workflow_line_violates("#            -- scripts/sl_desk_tick.py")

    def test_bare_double_dash_is_not_a_violation(self):
        """A `--` with no command after it is a YAML artifact, not an entrypoint."""
        assert not guard.workflow_line_violates("        --")


class TestAppImportsScripts:
    """E2: nothing under `app/` may import operator tooling."""

    def test_from_import_is_detected(self):
        tree = ast.parse("from scripts.bbref_bio_scraper import PlayerBio")
        assert guard._imports_scripts(tree) == [(1, "scripts.bbref_bio_scraper")]

    def test_plain_import_is_detected(self):
        tree = ast.parse("import scripts.sl_desk_tick")
        assert guard._imports_scripts(tree) == [(1, "scripts.sl_desk_tick")]

    def test_bare_scripts_package_import_is_detected(self):
        tree = ast.parse("import scripts")
        assert guard._imports_scripts(tree) == [(1, "scripts")]

    def test_unrelated_imports_are_ignored(self):
        tree = ast.parse("from app.services.stats import compute\nimport json")
        assert guard._imports_scripts(tree) == []

    def test_similarly_named_module_is_not_a_false_positive(self):
        """`scriptsomething` shares a prefix but is not the `scripts` package."""
        tree = ast.parse("import scriptsomething")
        assert guard._imports_scripts(tree) == []

    def test_relative_import_is_ignored(self):
        """A relative import can never escape `app/` into `scripts/`."""
        tree = ast.parse("from .scripts import helper")
        assert guard._imports_scripts(tree) == []


class TestRepoState:
    """The repo must pass its own guard — the check is worthless if it is vacuous."""

    def test_no_entrypoint_violations_in_tree(self):
        """No Fly toml or deploy workflow points a container entrypoint at scripts/."""
        assert guard.check_entrypoints() == []

    def test_no_unallowlisted_app_imports_of_scripts(self):
        """Only the recorded pre-existing violations may import operator tooling."""
        assert guard.check_app_imports() == []

    def test_allowlist_entries_still_exist_and_still_offend(self):
        """Guards the ratchet against rot in both directions.

        An allowlist entry that no longer imports `scripts.*` (or no longer exists) must
        be removed, or the boundary silently reopens for that file.
        """
        for known in guard._KNOWN_APP_IMPORTS_SCRIPTS:
            path = guard.REPO_ROOT / known
            assert path.is_file(), f"{known} is allowlisted but does not exist"
            tree = ast.parse(path.read_text(encoding="utf-8"))
            assert guard._imports_scripts(tree), (
                f"{known} no longer imports scripts/; remove it from the allowlist"
            )
