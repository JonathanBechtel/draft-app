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
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


# A file this long has outgrown a reviewable unit. Approximate by design.
THRESHOLD = 500

# No single change should add this many lines to one file, whatever its absolute size.
# Targets the "grew beyond a reviewable unit" failure directly.
DELTA_CAP = 300

_WAIVER_RE = re.compile(r"#\s*discipline:\s*file-size\b(?P<reason>.*)")


@dataclass(frozen=True)
class FileChange:
    """One file's line count before and after the change."""

    path: str
    old_lines: int
    new_lines: int
    old_path: str | None = None

    @property
    def delta(self) -> int:
        """Net lines added (negative when the file shrank)."""
        return self.new_lines - self.old_lines


def _git(*args: str) -> str:
    """Run a git command and return stdout, raising on failure."""
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=True
    ).stdout


def _count_lines_at_ref(ref: str, path: str) -> int:
    """Line count of ``path`` as of ``ref``; 0 if it did not exist there."""
    try:
        blob = _git("show", f"{ref}:{path}")
    except subprocess.CalledProcessError:
        return 0
    return len(blob.splitlines())


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
    for line in file.read_text(encoding="utf-8", errors="replace").splitlines():
        match = _WAIVER_RE.search(line)
        if match and match.group("reason").strip():
            return True
    return False


def collect_changes(against: str) -> list[FileChange]:
    """Return the ``app/**/*.py`` files this changeset touches, with before/after sizes.

    Rename and copy detection is on so a moved file reads as a move, not as a wholesale
    deletion plus a brand-new oversized file.
    """
    # Scope with a plain directory pathspec and filter extensions in Python: git's
    # default pathspec globbing does not treat `/` specially, so `app/*.py` would
    # silently match nested paths too.
    raw = _git("diff", "-M", "-C", "--name-status", against, "--", "app")

    changes: list[FileChange] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        status = fields[0]

        if status.startswith(("R", "C")) and len(fields) >= 3:
            old_path, new_path = fields[1], fields[2]
        else:
            old_path = new_path = fields[1]

        if status.startswith("D"):
            continue
        if not new_path.endswith(".py"):
            continue

        changes.append(
            FileChange(
                path=new_path,
                old_lines=_count_lines_at_ref(against, old_path),
                new_lines=_count_lines_on_disk(new_path),
                old_path=old_path if old_path != new_path else None,
            )
        )
    return changes


def evaluate(changes: list[FileChange]) -> tuple[list[str], int]:
    """Return (violation messages, net line delta across the changeset)."""
    net_delta = sum(change.delta for change in changes)
    violations: list[str] = []

    for change in changes:
        if _is_waived(change.path):
            continue

        is_new = change.old_lines == 0

        if is_new:
            # A new file is governed by the threshold alone. Writing a fresh 450-line
            # module is ordinary work; the delta cap below is about cramming lines into
            # a file that already exists, so applying it here would tax every new module.
            if change.new_lines > THRESHOLD:
                violations.append(
                    f"{change.path}: new file is {change.new_lines} lines "
                    f"(threshold {THRESHOLD}); start it decomposed"
                )
            continue

        if change.new_lines > THRESHOLD and change.delta > 0:
            violations.append(
                f"{change.path}: {change.old_lines} -> {change.new_lines} lines "
                f"(+{change.delta}); already over {THRESHOLD}, must not grow"
            )
        elif change.delta > DELTA_CAP:
            violations.append(
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
