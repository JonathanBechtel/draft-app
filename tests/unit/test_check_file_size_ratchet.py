"""Tests for the diff-scoped file-size ratchet.

The rule's hardest requirement is negative: it must *not* block the god-file decomposition it
exists to encourage. A naive per-file delta check fails a pure split — which would turn the
rule into an argument against cleaning up the very files it targets. Those cases are pinned
here alongside the ordinary ratchet behavior.
"""

from __future__ import annotations

import subprocess

import pytest

from tests.unit._script_loader import load_script


ratchet = load_script("check_file_size_ratchet")
FileChange = ratchet.FileChange
THRESHOLD = ratchet.THRESHOLD
DELTA_CAP = ratchet.DELTA_CAP


class TestRatchetVerdicts:
    """The four situations the spec's decision table enumerates."""

    def test_file_under_threshold_passes(self):
        """Size below the threshold is fine however it got there."""
        changes = [FileChange("app/a.py", old_lines=100, new_lines=200)]
        violations, _ = ratchet.evaluate(changes)
        assert violations == []

    def test_oversized_file_that_grows_fails(self):
        """The core ratchet: every touch of a god file must leave it no worse."""
        changes = [FileChange("app/big.py", old_lines=900, new_lines=950)]
        violations, _ = ratchet.evaluate(changes)
        assert len(violations) == 1
        assert "must not grow" in violations[0]
        assert "900 -> 950" in violations[0]

    def test_oversized_file_that_shrinks_passes(self):
        """Chipping away at a god file is exactly the encouraged direction."""
        changes = [FileChange("app/big.py", old_lines=900, new_lines=850)]
        violations, _ = ratchet.evaluate(changes)
        assert violations == []

    def test_oversized_file_left_unchanged_passes(self):
        """Touching a god file without changing its size is not a regression."""
        changes = [FileChange("app/big.py", old_lines=900, new_lines=900)]
        violations, _ = ratchet.evaluate(changes)
        assert violations == []

    def test_new_oversized_file_fails(self):
        """A file born over the threshold never gets the benefit of the ratchet."""
        changes = [FileChange("app/new.py", old_lines=0, new_lines=THRESHOLD + 1)]
        violations, _ = ratchet.evaluate(changes)
        assert len(violations) == 1
        assert "new file" in violations[0]

    def test_new_file_under_threshold_passes(self):
        """Adding a normal-sized module must stay frictionless."""
        changes = [FileChange("app/new.py", old_lines=0, new_lines=THRESHOLD - 1)]
        violations, _ = ratchet.evaluate(changes)
        assert violations == []


class TestDeltaCap:
    """No single change should add a wall of lines to one file, whatever its size."""

    def test_large_addition_to_a_small_file_fails(self):
        """Targets the 35,018-insertion merge shape directly."""
        changes = [FileChange("app/a.py", old_lines=10, new_lines=10 + DELTA_CAP + 1)]
        violations, _ = ratchet.evaluate(changes)
        assert len(violations) == 1
        assert f"cap {DELTA_CAP}" in violations[0]

    def test_addition_at_the_cap_passes(self):
        """The cap is inclusive; only exceeding it is a finding."""
        changes = [FileChange("app/a.py", old_lines=10, new_lines=10 + DELTA_CAP)]
        violations, _ = ratchet.evaluate(changes)
        assert violations == []

    def test_cap_does_not_apply_to_new_files(self):
        """A new module under the threshold must not be taxed by the growth cap.

        The cap targets cramming lines into a file that already exists. Writing a fresh
        450-line module is ordinary work, and the threshold is what governs it.
        """
        assert THRESHOLD - 1 > DELTA_CAP, "otherwise this case cannot arise"
        changes = [FileChange("app/new.py", old_lines=0, new_lines=THRESHOLD - 1)]
        violations, _ = ratchet.evaluate(changes)
        assert violations == []


class TestDoesNotBlockDecomposition:
    """The rule must not become an argument against splitting god files."""

    def test_pure_split_is_allowed_despite_per_file_findings(
        self, tmp_path, monkeypatch
    ):
        """5,000-line service -> shrunken original plus new modules of moved lines.

        Per-file this trips two findings (new files over the threshold), but the changeset
        adds no lines overall, so it is a redistribution and must pass.
        """
        changes = [
            FileChange("app/services/big.py", old_lines=5000, new_lines=800),
            FileChange("app/services/big_queries.py", old_lines=0, new_lines=2100),
            FileChange("app/services/big_compute.py", old_lines=0, new_lines=2100),
        ]
        violations, net_delta = ratchet.evaluate(changes)
        assert violations, "per-file checks should still notice the new large files"
        assert net_delta == 0

        monkeypatch.setattr(ratchet, "collect_changes", lambda against: changes)
        assert ratchet.main(["--enforce"]) == 0

    def test_growth_disguised_as_a_split_still_fails(self, monkeypatch):
        """Shrinking one file does not buy the right to grow another past the rule."""
        changes = [
            FileChange("app/services/big.py", old_lines=900, new_lines=1400),
            FileChange("app/services/other.py", old_lines=200, new_lines=190),
        ]
        violations, net_delta = ratchet.evaluate(changes)
        assert violations
        assert net_delta > 0

        monkeypatch.setattr(ratchet, "collect_changes", lambda against: changes)
        assert ratchet.main(["--enforce"]) == 1


class TestWaiver:
    """The escape hatch keeps the justification visible in review."""

    def test_justified_waiver_exempts_the_file(self, tmp_path, monkeypatch):
        """A reason-carrying marker exempts an otherwise failing file."""
        monkeypatch.chdir(tmp_path)
        target = tmp_path / "app" / "big.py"
        target.parent.mkdir(parents=True)
        target.write_text("# discipline: file-size generated table, not hand-edited\n")

        changes = [FileChange("app/big.py", old_lines=900, new_lines=950)]
        violations, _ = ratchet.evaluate(changes)
        assert violations == []

    def test_waiver_without_a_reason_is_rejected(self, tmp_path, monkeypatch):
        """A bare marker must not silence the rule."""
        monkeypatch.chdir(tmp_path)
        target = tmp_path / "app" / "big.py"
        target.parent.mkdir(parents=True)
        target.write_text("# discipline: file-size\n")

        changes = [FileChange("app/big.py", old_lines=900, new_lines=950)]
        violations, _ = ratchet.evaluate(changes)
        assert len(violations) == 1


class TestEnforcementModes:
    """Pre-commit warns while context is fresh; CI enforces against the merge base."""

    @pytest.fixture
    def failing_changes(self, monkeypatch):
        changes = [FileChange("app/big.py", old_lines=900, new_lines=1500)]
        monkeypatch.setattr(ratchet, "collect_changes", lambda against: changes)
        return changes

    def test_warn_mode_reports_but_exits_zero(self, failing_changes, capsys):
        """Pre-commit surfaces the finding without blocking the commit."""
        assert ratchet.main([]) == 0
        assert "WARNING" in capsys.readouterr().err

    def test_enforce_mode_fails(self, failing_changes, capsys):
        """CI turns the same finding into a failed build."""
        assert ratchet.main(["--enforce"]) == 1
        assert "ERROR" in capsys.readouterr().err

    def test_no_changes_passes(self, monkeypatch):
        """A changeset touching no app/ Python files is a no-op."""
        monkeypatch.setattr(ratchet, "collect_changes", lambda against: [])
        assert ratchet.main(["--enforce"]) == 0

    def test_git_failure_fails_closed_when_enforcing(self, monkeypatch, capsys):
        """A broken diff in CI must be a red gate, not a silent pass.

        Every other gate in this repo fails closed; a missing base ref waving the
        changeset through would make the ratchet quietly vacuous in exactly the
        runs it exists for.
        """

        def _boom(against):
            raise subprocess.CalledProcessError(128, ["git", "diff"])

        monkeypatch.setattr(ratchet, "collect_changes", _boom)
        assert ratchet.main(["--enforce"]) == 1
        assert "git diff failed" in capsys.readouterr().err

    def test_git_failure_stays_permissive_in_warn_mode(self, monkeypatch, capsys):
        """A local git hiccup must not block a commit the CI gate will still judge."""

        def _boom(against):
            raise subprocess.CalledProcessError(128, ["git", "diff"])

        monkeypatch.setattr(ratchet, "collect_changes", _boom)
        assert ratchet.main([]) == 0
        assert "git diff failed" in capsys.readouterr().err


class TestCollectChangesAgainstGit:
    """The git plumbing must read real diffs, including renames."""

    @pytest.fixture
    def git_repo(self, tmp_path, monkeypatch):
        """An initialized throwaway repo, checked out as the working directory."""
        monkeypatch.chdir(tmp_path)
        subprocess.run(["git", "init", "-q"], check=True)
        subprocess.run(["git", "config", "user.email", "t@t.t"], check=True)
        subprocess.run(["git", "config", "user.name", "t"], check=True)
        return tmp_path

    def test_detects_a_growing_file_in_a_real_repo(self, git_repo):
        """End-to-end over a throwaway git repo, not a mocked diff."""
        tmp_path = git_repo

        target = tmp_path / "app" / "svc.py"
        target.parent.mkdir(parents=True)
        target.write_text("\n".join(f"x = {i}" for i in range(600)) + "\n")
        subprocess.run(["git", "add", "-A"], check=True)
        subprocess.run(["git", "commit", "-qm", "base"], check=True)

        target.write_text("\n".join(f"x = {i}" for i in range(700)) + "\n")

        changes = ratchet.collect_changes("HEAD")
        assert len(changes) == 1
        assert changes[0].path == "app/svc.py"
        assert changes[0].old_lines == 600
        assert changes[0].new_lines == 700

        violations, _ = ratchet.evaluate(changes)
        assert len(violations) == 1
        assert "must not grow" in violations[0]

    def test_rename_is_not_read_as_a_new_oversized_file(self, git_repo):
        """Moving a god file must not be punished as if it were newly written."""
        tmp_path = git_repo

        source = tmp_path / "app" / "svc.py"
        source.parent.mkdir(parents=True)
        body = "\n".join(f"x = {i}" for i in range(900)) + "\n"
        source.write_text(body)
        subprocess.run(["git", "add", "-A"], check=True)
        subprocess.run(["git", "commit", "-qm", "base"], check=True)

        source.unlink()
        (tmp_path / "app" / "renamed.py").write_text(body)
        subprocess.run(["git", "add", "-A"], check=True)

        changes = ratchet.collect_changes("HEAD")
        moved = [c for c in changes if c.path == "app/renamed.py"]
        assert len(moved) == 1
        assert moved[0].old_lines == 900, "rename detection should find the old size"
        assert moved[0].delta == 0

        violations, _ = ratchet.evaluate(changes)
        assert violations == []

    def test_split_that_deletes_the_original_is_allowed(self, git_repo):
        """The decomposition shape git does *not* detect as a rename.

        A 5,000-line service split ten ways shares too little with any one output for
        rename detection to fire, so git reports a deletion plus ten additions. Skipping
        deletions made that read as +5,000 lines of pure growth and fail — the rule
        blocking the work it exists to encourage. The sibling rename test above covers
        only the shape where detection *does* fire, which is why this one was missed.
        """
        tmp_path = git_repo

        source = tmp_path / "app" / "svc.py"
        source.parent.mkdir(parents=True)
        source.write_text("\n".join(f"x = {i}" for i in range(5000)) + "\n")
        subprocess.run(["git", "add", "-A"], check=True)
        subprocess.run(["git", "commit", "-qm", "base"], check=True)

        # Ten distinct 500-line modules; no single one resembles the 5,000-line original.
        source.unlink()
        for part in range(10):
            body = "\n".join(f"y{part} = {i}" for i in range(500)) + "\n"
            (tmp_path / "app" / f"part{part}.py").write_text(body)
        subprocess.run(["git", "add", "-A"], check=True)

        changes = ratchet.collect_changes("HEAD")
        deleted = [c for c in changes if c.path == "app/svc.py"]
        assert len(deleted) == 1, "the deleted original must appear in the changeset"
        assert deleted[0].old_lines == 5000
        assert deleted[0].new_lines == 0

        violations, net_delta = ratchet.evaluate(changes)
        assert net_delta <= 0, f"a pure split must not read as growth (got {net_delta:+d})"
        assert ratchet.main(["--against", "HEAD", "--enforce"]) == 0

        # The deletion itself must not be reported as a finding.
        assert not any("app/svc.py" in violation for violation in violations)

    def test_a_large_deletion_does_not_buy_growth_of_an_existing_file(self, git_repo):
        """Caught in review of the deletion fix (codex, PR #677).

        Counting deletions widened the `net_delta <= 0` allowance: deleting an obsolete
        1,200-line module while growing a 900-line service to 1,400 netted -700 and
        suppressed a real violation. The converse test below only covered a deletion
        *smaller* than the growth, so it did not catch it.

        Deletion credit is now capped at the lines added in new files, so a deletion can
        offset creation but never growth of a file that already existed.
        """
        tmp_path = git_repo
        (tmp_path / "app").mkdir()
        god = tmp_path / "app" / "god.py"
        obsolete = tmp_path / "app" / "obsolete.py"
        god.write_text("\n".join(f"x = {i}" for i in range(900)) + "\n")
        obsolete.write_text("\n".join(f"z = {i}" for i in range(1200)) + "\n")
        subprocess.run(["git", "add", "-A"], check=True)
        subprocess.run(["git", "commit", "-qm", "base"], check=True)

        god.write_text("\n".join(f"x = {i}" for i in range(1400)) + "\n")
        obsolete.unlink()
        subprocess.run(["git", "add", "-A"], check=True)

        violations, net_delta = ratchet.evaluate(ratchet.collect_changes("HEAD"))
        assert any("god.py" in violation for violation in violations)
        assert net_delta > 0, (
            "a deletion that was not redistributed into new files must not be credited"
        )
        assert ratchet.main(["--against", "HEAD", "--enforce"]) == 1

    def test_deleting_a_file_does_not_license_growing_a_god_file(self, git_repo):
        """Counting deletions must not become a way to buy growth elsewhere.

        The net-delta allowance is deliberately generous, but a changeset whose *only*
        shrinkage is an unrelated deletion should still be judged on what it grew.
        """
        tmp_path = git_repo
        (tmp_path / "app").mkdir()
        god = tmp_path / "app" / "god.py"
        spare = tmp_path / "app" / "spare.py"
        god.write_text("\n".join(f"x = {i}" for i in range(900)) + "\n")
        spare.write_text("\n".join(f"y = {i}" for i in range(100)) + "\n")
        subprocess.run(["git", "add", "-A"], check=True)
        subprocess.run(["git", "commit", "-qm", "base"], check=True)

        god.write_text("\n".join(f"x = {i}" for i in range(1100)) + "\n")
        spare.unlink()
        subprocess.run(["git", "add", "-A"], check=True)

        violations, net_delta = ratchet.evaluate(ratchet.collect_changes("HEAD"))
        assert any("god.py" in violation for violation in violations)
        assert net_delta > 0, "+200 grown against -100 deleted is still net growth"
        assert ratchet.main(["--against", "HEAD", "--enforce"]) == 1

    def test_measures_from_the_merge_base_not_the_base_branch_tip(self, git_repo):
        """Growth on the base branch after divergence must not be attributed here.

        Caught in review. Two-dot `git diff main` compares main's *tip* with the working
        tree, so once main advances the comparison mixes in commits this branch never made.
        Here main grows a file 600 -> 900 while the branch adds 10 lines to its own 600-line
        copy: measured from the tip that reads as a 290-line *shrink*, which would happily
        mask real growth. Measured from the merge base it is the true +10.
        """
        tmp_path = git_repo
        target = tmp_path / "app" / "svc.py"
        target.parent.mkdir(parents=True)

        def write(n: int) -> None:
            target.write_text("\n".join(f"x = {i}" for i in range(n)) + "\n")

        write(600)
        subprocess.run(["git", "add", "-A"], check=True)
        subprocess.run(["git", "commit", "-qm", "base"], check=True)
        subprocess.run(["git", "branch", "-M", "main"], check=True)
        subprocess.run(["git", "checkout", "-qb", "feature"], check=True)

        # main advances independently, well past the threshold.
        subprocess.run(["git", "checkout", "-q", "main"], check=True)
        write(900)
        subprocess.run(["git", "add", "-A"], check=True)
        subprocess.run(["git", "commit", "-qm", "main grows"], check=True)

        # The branch makes its own small change from the 600-line ancestor.
        subprocess.run(["git", "checkout", "-q", "feature"], check=True)
        write(610)

        changes = ratchet.collect_changes("main")
        assert len(changes) == 1
        assert changes[0].old_lines == 600, (
            "should measure from the merge base, not main's tip"
        )
        assert changes[0].new_lines == 610
        assert changes[0].delta == 10

        violations, _ = ratchet.evaluate(changes)
        assert len(violations) == 1, (
            "a 600->610 growth past the threshold is a real finding"
        )
        assert "must not grow" in violations[0]
