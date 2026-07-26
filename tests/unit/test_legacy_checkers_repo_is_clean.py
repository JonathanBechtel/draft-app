"""Repo-wide clean checks for the two AST checkers that predate the Phase 0 guardrails.

Why this exists
---------------
``check_request_transaction_policy.py`` and ``check_route_conventions.py`` run only as
pre-commit hooks. Hooks live on the developer's machine: ``git commit --no-verify`` skips
them, and they do not exist at all in a checkout where nobody ran ``pre-commit install`` —
which is the normal state of a fresh agent worktree. Nothing re-checked them server-side, so
both were advisory rather than enforced.

``check_unscoped_delete.py`` shipped with exactly this test (see
``tests/unit/test_check_unscoped_delete.py::TestRepositoryIsClean``), which is what makes it
a real gate: pytest runs in CI, so a violation reaching ``main`` fails the build whatever the
committer's hooks were doing. These two never got the same treatment. This closes that,
using the same pattern rather than a new mechanism.

Scope note: these assert the tree is clean, not that the checkers are correct — their unit
behavior is untested, which is separate debt. A clean-tree assertion is what converts a local
hook into a merge gate, and that is the gap being closed here.
"""

from __future__ import annotations

from pathlib import Path

from tests.unit._script_loader import SCRIPTS_DIR, load_script


REPO_ROOT = SCRIPTS_DIR.parent

transaction_policy = load_script("check_request_transaction_policy")
route_conventions = load_script("check_route_conventions")


def _python_files(*relative_dirs: str) -> list[Path]:
    """Return every ``.py`` file under the given repo-relative directories, sorted."""
    files: list[Path] = []
    for relative in relative_dirs:
        files.extend(sorted((REPO_ROOT / relative).rglob("*.py")))
    return files


def test_request_code_has_no_commit_or_rollback() -> None:
    """Routes and services must leave transaction boundaries to the caller.

    Mirrors the hook's own glob (``app/routes``, ``app/services``).
    """
    violations = transaction_policy._find_violations(
        _python_files("app/routes", "app/services")
    )
    assert violations == [], "\n".join(violations)


def test_routes_follow_the_declared_conventions() -> None:
    """Every route keeps response_model / status_code / DI / TemplateResponse form."""
    violations: list[str] = []
    for path in _python_files("app/routes"):
        # `.format()`, not `str()`: the dataclass repr buries the file and line in noise.
        violations.extend(v.format() for v in route_conventions._check_file(path))
    assert violations == [], "\n".join(violations)
