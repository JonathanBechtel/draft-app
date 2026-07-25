"""Tests for the diff-scoped file-size ratchet.

The rule's hardest requirement is negative: it must *not* block the god-file decomposition it
exists to encourage. A naive per-file delta check fails a pure split — which would turn the
rule into an argument against cleaning up the very files it targets. Those cases are pinned
here alongside the ordinary ratchet behavior.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_file_size_ratchet.py"


def _load_checker():
    """Import the checker script by path (scripts/ is not an installed package)."""
    spec = importlib.util.spec_from_file_location("check_file_size_ratchet", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ratchet = _load_checker()
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


class TestCollectChangesAgainstGit:
    """The git plumbing must read real diffs, including renames."""

    def test_detects_a_growing_file_in_a_real_repo(self, tmp_path, monkeypatch):
        """End-to-end over a throwaway git repo, not a mocked diff."""
        import subprocess

        monkeypatch.chdir(tmp_path)
        subprocess.run(["git", "init", "-q"], check=True)
        subprocess.run(["git", "config", "user.email", "t@t.t"], check=True)
        subprocess.run(["git", "config", "user.name", "t"], check=True)

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

    def test_rename_is_not_read_as_a_new_oversized_file(self, tmp_path, monkeypatch):
        """Moving a god file must not be punished as if it were newly written."""
        import subprocess

        monkeypatch.chdir(tmp_path)
        subprocess.run(["git", "init", "-q"], check=True)
        subprocess.run(["git", "config", "user.email", "t@t.t"], check=True)
        subprocess.run(["git", "config", "user.name", "t"], check=True)

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
