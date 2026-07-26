"""Diff-scoped file-size ratchet for ``app/`` Python modules.

Failure this descends from
--------------------------
Six central Event Desk files total ~7,677 lines and ``summer_league_explorer_service.py`` is
~5,000 lines on its own. The Summer League failure record names *complexity beyond a reviewable
unit* as a root cause, and the initial Desk merge — 104 files, 35,018 insertions — is the shape
it means. See ``docs/plans/programmatic-code-discipline.md`` §1.4.

The rule — a delta, not an absolute
-----------------------------------
Sixty files in ``app/`` already exceed the threshold, so an absolute limit would be either
ignored or a permanent wall of noise. Instead the rule measures the *change*, mirroring the
repo's existing ``make coverage.diff`` habit. Git already knows the before-size, so there is no
baseline file to maintain or resolve conflicts in.

For each ``app/**/*.py`` file the changeset touches:

===============================================  =========
Situation                                        Verdict
===============================================  =========
Ends **under** the threshold                     pass
Already over, and the change makes it larger     **fail**
Already over, and the change shrinks it          pass
**New** file over the threshold                  **fail**
Grew by more than the per-file delta cap         **fail**
===============================================  =========

The ratchet therefore applies itself gradually: every touch of a god file must leave it no
worse, and most touches leave it better.

Do not block the decomposition this rule exists to encourage
------------------------------------------------------------
Splitting a 5,000-line service into modules creates new files full of *moved* lines. A naive
per-file check fails exactly the refactor we want, which would make the rule an argument
*against* cleaning up god files. Two mechanisms prevent that:

* **Rename/copy detection** (``git diff -M -C``) so moved files are not read as additions.
* **Net-change evaluation across the whole changeset**: if the touched files collectively did
  not grow, the change is a redistribution and passes regardless of per-file movement.

Escape hatch
------------
A file containing a ``# discipline: file-size <reason>`` comment is exempt, with the
justification visible in review.

Usage::

    python scripts/check_file_size_ratchet.py               # vs HEAD, warn only
    python scripts/check_file_size_ratchet.py --against origin/main --enforce
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from _discipline import text_has_reasoned_waiver


# A file this long has outgrown a reviewable unit. Approximate by design.
THRESHOLD = 500

# No single change should add this many lines to one file, whatever its absolute size.
# Targets the "grew beyond a reviewable unit" failure directly.
DELTA_CAP = 300

# The escape-hatch slug; syntax and the mandatory-reason rule live in _discipline.py.
_RULE = "file-size"


@dataclass(frozen=True)
class FileChange:
    """One file's line count before and after the change."""

    path: str
    old_lines: int
    new_lines: int

    @property
    def delta(self) -> int:
        """Net lines added (negative when the file shrank)."""
        return self.new_lines - self.old_lines


def _git(*args: str) -> str:
    """Run a git command and return stdout, raising on failure."""
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=True
    ).stdout


def _merge_base(ref: str) -> str:
    """Resolve the merge base of ``ref`` and HEAD, falling back to ``ref`` itself.

    Two-dot ``git diff <ref>`` compares the *tip* of ``ref`` with the working tree, so once
    the base branch advances past the point this branch diverged, commits made only on the
    base show up in the comparison — inflating or masking this branch's real growth. The
    repo's sibling gate (``diff-cover --compare-branch``) resolves the merge base for the
    same reason.
    """
    try:
        return _git("merge-base", ref, "HEAD").strip() or ref
    except subprocess.CalledProcessError:
        return ref


def _line_deltas(against: str) -> dict[str, tuple[int, int]]:
    """Map each changed path to its ``(added, deleted)`` line counts.

    One git invocation for the whole changeset. The previous shape spawned a separate
    ``git show <ref>:<path>`` per file to recover the old size, which is N+1 subprocesses
    on every commit — and this hook runs with ``pass_filenames: false``, so it re-derives
    the full changeset each time.
    """
    # -z keeps renames unambiguous: without it a rename renders as `old => new` in the
    # path column, which would not key back to the path --name-status reported. Under -z
    # a rename record leaves the path field empty and emits old then new as their own
    # NUL-terminated tokens.
    raw = _git("diff", "-M", "-C", "--numstat", "-z", against, "--", "app")
    tokens = raw.split("\0")

    deltas: dict[str, tuple[int, int]] = {}
    index = 0
    while index < len(tokens):
        record = tokens[index]
        index += 1
        if not record:
            continue
        fields = record.split("\t")
        if len(fields) < 3:
            continue
        added, deleted, path = fields[0], fields[1], fields[2]
        if not path:  # rename/copy: the next two tokens are the old and new paths
            if index + 1 >= len(tokens):
                break
            path = tokens[index + 1]
            index += 2
        if added == "-" or deleted == "-":  # binary; no line counts to reason about
            continue
        deltas[path] = (int(added), int(deleted))
    return deltas


def _count_lines_on_disk(path: str) -> int:
    """Line count of ``path`` in the working tree; 0 if deleted."""
    file = Path(path)
    if not file.is_file():
        return 0
    return len(file.read_text(encoding="utf-8", errors="replace").splitlines())


def _is_waived(path: str) -> bool:
    """True if the file carries a justified ``# discipline: file-size`` comment."""
    file = Path(path)
    if not file.is_file():
        return False
    return text_has_reasoned_waiver(
        file.read_text(encoding="utf-8", errors="replace"), _RULE
    )


def collect_changes(against: str) -> list[FileChange]:
    """Return the ``app/**/*.py`` files this changeset touches, with before/after sizes.

    Rename and copy detection is on so a moved file reads as a move, not as a wholesale
    deletion plus a brand-new oversized file.
    """
    # Always measure from where this branch diverged, never from the base branch's tip.
    base = _merge_base(against)

    # Scope with a plain directory pathspec and filter extensions in Python: git's
    # default pathspec globbing does not treat `/` specially, so `app/*.py` would
    # silently match nested paths too.
    raw = _git("diff", "-M", "-C", "--name-status", base, "--", "app")
    deltas = _line_deltas(base)

    changes: list[FileChange] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        status = fields[0]

        # Rename/copy records carry both paths; the new one is what exists on disk.
        new_path = (
            fields[2]
            if status.startswith(("R", "C")) and len(fields) >= 3
            else fields[1]
        )

        if status.startswith("D"):
            continue
        if not new_path.endswith(".py"):
            continue

        # Recover the old size arithmetically rather than fetching the old blob:
        # old = new - added + deleted. Falling back to (0, 0) leaves old == new,
        # which reads as "unchanged size" — the safe direction if git reported a
        # path here that --numstat did not.
        added, deleted = deltas.get(new_path, (0, 0))
        new_lines = _count_lines_on_disk(new_path)

        changes.append(
            FileChange(
                path=new_path,
                old_lines=new_lines - added + deleted,
                new_lines=new_lines,
            )
        )
    return changes


def evaluate(changes: list[FileChange]) -> tuple[list[str], int]:
    """Return (violation messages, net line delta across the changeset)."""
    net_delta = sum(change.delta for change in changes)
    violations: list[str] = []

    def report(message: str) -> None:
        """Record a violation unless the file carries a justified waiver.

        The waiver read happens here, not up front, so the common case — a touched file
        nowhere near the threshold — never opens the file at all.
        """
        if not _is_waived(change.path):
            violations.append(message)

    for change in changes:
        is_new = change.old_lines == 0

        if is_new:
            # A new file is governed by the threshold alone. Writing a fresh 450-line
            # module is ordinary work; the delta cap below is about cramming lines into
            # a file that already exists, so applying it here would tax every new module.
            if change.new_lines > THRESHOLD:
                report(
                    f"{change.path}: new file is {change.new_lines} lines "
                    f"(threshold {THRESHOLD}); start it decomposed"
                )
            continue

        if change.new_lines > THRESHOLD and change.delta > 0:
            report(
                f"{change.path}: {change.old_lines} -> {change.new_lines} lines "
                f"(+{change.delta}); already over {THRESHOLD}, must not grow"
            )
        elif change.delta > DELTA_CAP:
            report(
                f"{change.path}: +{change.delta} lines in one change "
                f"(cap {DELTA_CAP}); split it into reviewable pieces"
            )

    return violations, net_delta


def main(argv: list[str] | None = None) -> int:
    """Check the changeset; return 1 only when enforcing and violations remain."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--against",
        default="HEAD",
        help="Git ref to compare against (CI passes the PR merge base).",
    )
    parser.add_argument(
        "--enforce",
        action="store_true",
        help="Exit non-zero on violations. Without it, findings are warnings.",
    )
    args = parser.parse_args(argv)

    try:
        changes = collect_changes(args.against)
    except subprocess.CalledProcessError as exc:  # pragma: no cover - CI plumbing
        sys.stderr.write(f"file-size ratchet: git diff failed: {exc}\n")
        return 0

    if not changes:
        return 0

    violations, net_delta = evaluate(changes)
    if not violations:
        return 0

    # A pure decomposition moves lines between files without adding any. Passing it is the
    # point: the rule must not become an argument against splitting god files.
    if net_delta <= 0:
        sys.stderr.write(
            f"file-size ratchet: {len(violations)} per-file finding(s), but the changeset "
            f"is net {net_delta:+d} lines across app/ — treating it as a redistribution "
            "and allowing it.\n"
        )
        return 0

    label = "ERROR" if args.enforce else "WARNING"
    sys.stderr.write(
        "\n".join(
            [
                f"{label}: file-size ratchet ({len(violations)} finding(s), "
                f"changeset is net {net_delta:+d} lines).",
                "",
                "Complexity beyond a reviewable unit is a named root cause of the Summer",
                "League failures — see docs/plans/programmatic-code-discipline.md §1.4.",
                "",
                *sorted(violations),
                "",
                "Split the change, or exempt the file with a justification:",
                "    # discipline: file-size <reason>",
                "",
            ]
        )
    )
    return 1 if args.enforce else 0


if __name__ == "__main__":
    raise SystemExit(main())
