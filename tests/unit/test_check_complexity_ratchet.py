"""Tests for the per-file complexity ratchet.

The rule this closes: Ruff's ``per-file-ignores`` silences a rule for a whole file, so a new
complexity-15 function inside an already-baselined module passes ``ruff check`` untouched.
Raised in review of PR #673. These tests pin that counts may fall but never rise, and that the
baseline is compared per ``(file, rule)`` rather than per file.
"""

from __future__ import annotations

import json

from tests.unit._script_loader import load_script


ratchet = load_script("check_complexity_ratchet")


class TestComparison:
    """Counts may fall, never rise."""

    def test_unchanged_counts_pass(self):
        """A tree matching its baseline is clean."""
        counts = {"app/a.py": {"C901": 3}}
        regressions, improvements = ratchet.compare(counts, counts)
        assert regressions == []
        assert improvements == []

    def test_growth_inside_a_baselined_file_is_a_regression(self):
        """The gap this whole script exists to close.

        `app/a.py` is already baselined for C901, so ruff would stay silent. A fourth
        finding must still fail.
        """
        regressions, _ = ratchet.compare(
            {"app/a.py": {"C901": 4}}, {"app/a.py": {"C901": 3}}
        )
        assert len(regressions) == 1
        assert "4 findings, baseline allows 3" in regressions[0]

    def test_a_new_file_is_a_regression(self):
        """A module born complex never gets baseline forgiveness."""
        regressions, _ = ratchet.compare({"app/new.py": {"C901": 1}}, {})
        assert len(regressions) == 1
        assert "new file for this rule" in regressions[0]

    def test_a_new_rule_in_a_baselined_file_is_a_regression(self):
        """Baselining a file for C901 must not silently forgive PLR0913 too."""
        regressions, _ = ratchet.compare(
            {"app/a.py": {"C901": 3, "PLR0913": 1}}, {"app/a.py": {"C901": 3}}
        )
        assert len(regressions) == 1
        assert "PLR0913" in regressions[0]

    def test_fewer_findings_are_reported_as_improvements(self):
        """A dropped count is an improvement — surfaced so main() can demand a lock-in."""
        regressions, improvements = ratchet.compare(
            {"app/a.py": {"C901": 1}}, {"app/a.py": {"C901": 3}}
        )
        assert regressions == []
        assert improvements == ["app/a.py [C901]: 3 -> 1"]

    def test_fully_cleaned_file_is_an_improvement_not_a_regression(self):
        """A file that drops out of the report entirely is progress."""
        regressions, improvements = ratchet.compare({}, {"app/a.py": {"C901": 3}})
        assert regressions == []
        assert improvements == ["app/a.py [C901]: 3 -> 0"]

    def test_regressions_and_improvements_are_independent(self):
        """Cleaning one rule does not buy the right to grow another."""
        regressions, improvements = ratchet.compare(
            {"app/a.py": {"C901": 1, "PLR0915": 5}},
            {"app/a.py": {"C901": 3, "PLR0915": 4}},
        )
        assert len(regressions) == 1 and "PLR0915" in regressions[0]
        assert improvements == ["app/a.py [C901]: 3 -> 1"]


class TestStaleEntriesFail:
    """A stale baseline entry is silent regression headroom, not a free pass.

    Mirrors the stale-entry tests on the FK-coverage and import-contract baselines
    (docs/plans/programmatic-code-discipline.md §3.4b): an improvement must be locked
    in via ``make lint.complexity.update`` in the same change that earned it.
    """

    def _run_main(self, monkeypatch, current, baseline):
        monkeypatch.setattr(ratchet, "_current_counts", lambda: current)
        monkeypatch.setattr(ratchet, "_load_baseline", lambda: baseline)
        return ratchet.main([])

    def test_stale_entry_fails(self, monkeypatch, capsys):
        """A count below its baseline entry fails until the baseline is updated."""
        exit_code = self._run_main(
            monkeypatch, {"app/a.py": {"C901": 1}}, {"app/a.py": {"C901": 3}}
        )
        assert exit_code == 1
        err = capsys.readouterr().err
        assert "stale" in err
        assert "lint.complexity.update" in err

    def test_regressions_take_precedence_over_staleness(self, monkeypatch, capsys):
        """When both exist, the regression message is the one that surfaces."""
        exit_code = self._run_main(
            monkeypatch,
            {"app/a.py": {"C901": 1}, "app/b.py": {"C901": 5}},
            {"app/a.py": {"C901": 3}, "app/b.py": {"C901": 4}},
        )
        assert exit_code == 1
        assert "beyond the baseline" in capsys.readouterr().err

    def test_exact_match_passes(self, monkeypatch):
        """A tree exactly at its baseline exits clean."""
        counts = {"app/a.py": {"C901": 3}}
        assert self._run_main(monkeypatch, counts, dict(counts)) == 0


class TestBaselineFile:
    """The committed baseline must describe the tree it ships with."""

    def test_baseline_exists_and_is_wellformed(self):
        """A missing or malformed baseline would make the ratchet silently vacuous."""
        payload = json.loads(ratchet.BASELINE_PATH.read_text(encoding="utf-8"))
        assert "_comment" in payload, (
            "baseline should explain itself to whoever opens it"
        )
        counts = payload["counts"]
        assert counts, "empty baseline would forgive nothing and flag everything"
        for path, rules in counts.items():
            assert path.startswith(("app/", "scripts/")), path
            for rule, count in rules.items():
                assert rule in ratchet.RULES, rule
                assert isinstance(count, int) and count > 0

    def test_current_tree_matches_baseline_exactly(self):
        """The repo must pass its own ratchet — no regressions, no stale entries."""
        regressions, improvements = ratchet.compare(
            ratchet._current_counts(), ratchet._load_baseline()
        )
        assert regressions == [], "\n".join(regressions)
        assert improvements == [], (
            "stale baseline entries grant silent regression headroom — run "
            "`make lint.complexity.update`:\n" + "\n".join(improvements)
        )
