"""Stale-module-path checker for the phase-4 service reorganization (#797).

Failure this descends from
--------------------------
Phase 4 (PR #793) moved the Summer League service package from
``app/services/summer_league/`` to the ``stats/ backbone/ ingest/ sources/``
layering, as a pure ``git mv``. The imports were rewritten; 37 references in
*prose* -- runbooks, comments, docstrings, operator docs -- were not. An
operator following a runbook that names a module path which no longer imports
loses the time it takes to discover the rename, and the doc looks maintained
while being wrong. Ticket #797 swept all 37.

Nothing then stopped them coming back. This is that guard, and it exists
because a sweep with no ratchet behind it is a one-time cleanup, not a
property of the repo: the next person writing a runbook from memory
reintroduces the old path, and the only thing that would catch it is another
manual audit. See ``docs/plans/programmatic-code-discipline.md`` for the
general shape.

The rule
--------
Neither ``app.services.summer_league.<x>`` (import form) nor
``app/services/summer_league/`` (path form) may appear in any tracked ``.py``,
``.toml``, or ``.md`` file.

The one exemption: ``docs/plans/``. Those are historical planning documents --
specs, retrospectives, and the phase-4 conversion plan itself, which *must*
name the old path to describe the move it proposes. Rewriting them would
falsify the record. #797 deliberately left them, and this mirrors that
decision rather than re-litigating it.

Non-vacuity
-----------
A guard that scans nothing passes for the wrong reason -- the failure mode
``feedback_guards_must_be_nonvacuous_and_mirrored`` describes. This checker
therefore refuses to report success unless it actually examined a non-empty
file set, and says how many files it read.

Usage::

    python scripts/check_stale_service_paths.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

SCANNED_SUFFIXES = {".py", ".toml", ".md"}

# Historical planning documents must keep naming the pre-move path to describe
# the move. Matched against the repo-relative POSIX path.
EXEMPT_PREFIXES = ("docs/plans/",)

# Import form and filesystem form of the retired package.
STALE_PATTERN = re.compile(
    r"app\.services\.summer_league\.|app/services/summer_league/"
)

# This checker's own docstring names the retired path to explain itself.
SELF_PATH = "scripts/check_stale_service_paths.py"


def _tracked_files() -> list[str]:
    """Return every git-tracked repo-relative path, as POSIX strings."""
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [path for path in result.stdout.split("\0") if path]


def main() -> int:
    """Report any stale ``app/services/summer_league`` reference outside docs/plans/."""
    candidates = [
        path
        for path in _tracked_files()
        if Path(path).suffix in SCANNED_SUFFIXES
        and not path.startswith(EXEMPT_PREFIXES)
        and path != SELF_PATH
    ]

    if not candidates:
        print(
            "check_stale_service_paths: FAIL — scanned 0 files. The guard found "
            "nothing to check, which means it is broken, not that the repo is clean.",
            file=sys.stderr,
        )
        return 2

    violations: list[str] = []
    for path in candidates:
        try:
            text = (REPO_ROOT / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if STALE_PATTERN.search(line):
                violations.append(f"{path}:{lineno}: {line.strip()}")

    if violations:
        print(
            "check_stale_service_paths: FAIL — stale "
            "app/services/summer_league references (phase 4 moved that package "
            "to app/services/{stats,backbone,ingest,sources}/; see #797):\n",
            file=sys.stderr,
        )
        for violation in violations:
            print(f"  {violation}", file=sys.stderr)
        print(
            "\nUpdate the path. If the reference is genuinely historical, it "
            "belongs under docs/plans/, the one exempt tree.",
            file=sys.stderr,
        )
        return 1

    print(
        f"check_stale_service_paths: OK "
        f"({len(candidates)} files scanned, docs/plans/ exempt)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
