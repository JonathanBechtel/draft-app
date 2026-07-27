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
        assert not guard.workflow_line_violates(
            "              -- -m app.cli.sl_desk_tick"
        )

    def test_commented_tail_is_not_a_violation(self):
        assert not guard.workflow_line_violates(
            "#            -- scripts/sl_desk_tick.py"
        )

    def test_bare_double_dash_is_not_a_violation(self):
        """A `--` with no command after it is a YAML artifact, not an entrypoint."""
        assert not guard.workflow_line_violates("        --")

    def test_inline_single_line_command_is_a_violation(self):
        """The guard must not depend on `--` starting its own physical line.

        A whole `flyctl machine run ... -- scripts/job.py` written on one line
        deploys an operator script exactly like the wrapped form does.
        """
        assert guard.workflow_line_violates(
            'flyctl machine run "$IMG" --app prod -- scripts/job.py'
        )

    def test_flag_is_not_mistaken_for_a_command_tail(self):
        """`--app`/`--entrypoint` have no space after `--`, so they are flags."""
        assert not guard.workflow_line_violates("--app draft-app-prod")

    def test_ci_runner_script_invocation_is_not_a_violation(self):
        """`scripts/` run *on the runner* is the correct place for operator tooling.

        Only container entrypoints are E1's business; flagging these would make
        the guard unsatisfiable for the deploy workflow's seeding steps.
        """
        assert not guard.workflow_line_violates("python scripts/seed_nba_teams.py")
        assert not guard.workflow_line_violates(
            "python scripts/verify_cron_image_digests.py --app draft-app-prod"
        )


class TestContinuationFolding:
    """E1: shell backslash-continuations must fold before the tail is matched."""

    def test_tail_split_across_a_continuation_is_caught(self):
        r"""`-- \` on one line and the script on the next is still an entrypoint.

        Matching physical lines only made the guard depend on YAML wrapping, so a
        harmless reformat could silently reopen the boundary.
        """
        folded = guard.join_continuations(
            [
                '  flyctl machine run "$IMG" \\',
                "    --app prod \\",
                "    -- \\",
                "    scripts/job.py",
            ]
        )
        assert len(folded) == 1
        assert guard.workflow_line_violates(folded[0][1])

    def test_the_shipped_module_form_folds_clean(self):
        """The real workflow shape must stay green after folding."""
        folded = guard.join_continuations(
            [
                '  flyctl machine run "$APP_IMAGE" \\',
                '    --entrypoint "/app/.venv/bin/python" \\',
                "    -- -m app.cli.sl_desk_tick",
            ]
        )
        assert len(folded) == 1
        assert not guard.workflow_line_violates(folded[0][1])

    def test_folding_reports_the_starting_line_number(self):
        """Violations should point at where the command began, not where it ended."""
        folded = guard.join_continuations(["a", "b \\", "c", "d"])
        assert folded == [(1, "a"), (2, "b c"), (4, "d")]


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

        An allowlisted import that is gone (or a file that no longer exists) must be
        removed, or the exemption outlives the dependency it was granted for.
        """
        for known, allowed in guard._KNOWN_APP_IMPORTS_SCRIPTS.items():
            path = guard.REPO_ROOT / known
            assert path.is_file(), f"{known} is allowlisted but does not exist"
            tree = ast.parse(path.read_text(encoding="utf-8"))
            still = {name for _, name in guard._imports_scripts(tree)}
            assert allowed <= still, (
                f"{known} no longer imports {sorted(allowed - still)}; "
                "remove them from the allowlist"
            )

    def test_allowlist_is_keyed_by_module_not_by_file(self):
        """The exemption must name specific imports, so a new one is still caught.

        A file-level allowlist would let a fourth `scripts.*` import appear inside
        an already-listed runtime job with the check staying silent -- precisely
        the rot this guard exists to catch.
        """
        assert isinstance(guard._KNOWN_APP_IMPORTS_SCRIPTS, dict)
        for allowed in guard._KNOWN_APP_IMPORTS_SCRIPTS.values():
            assert allowed, "an empty allowlist entry exempts the whole file"
            assert all(m.startswith("scripts") for m in allowed)

    def test_allowlist_is_empty(self):
        """The boundary is fully closed; re-opening it must be a deliberate change.

        #688 lifted the roster cron's last three `scripts.*` imports into
        `app/services/`. The ratchet may only shrink, so a future entry here is a
        decision to argue for in review -- not something that lands quietly.
        """
        assert guard._KNOWN_APP_IMPORTS_SCRIPTS == {}

    def test_a_new_import_in_an_allowlisted_file_is_still_a_violation(self):
        """The regression codex caught: baseline by import, not by file.

        Exercised against a synthetic allowlist rather than the live one, which is
        empty now that the boundary is closed. The rule has to keep holding for
        whatever entry is baselined next, so the test cannot depend on one existing.
        """
        allowed = frozenset({"scripts.already_baselined"})
        tree = ast.parse(
            "from scripts.already_baselined import thing\n"
            "from scripts.some_new_tool import other\n"
        )

        offenders = {
            name for _, name in guard._imports_scripts(tree) if name not in allowed
        }
        assert offenders == {"scripts.some_new_tool"}
