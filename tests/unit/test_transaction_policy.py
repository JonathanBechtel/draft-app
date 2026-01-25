"""Policy tests to keep request code aligned with transaction conventions."""

from __future__ import annotations

import ast
from pathlib import Path


def test_request_bounded_code_has_no_explicit_commit_or_rollback() -> None:
    """Request-bounded code should not call commit()/rollback() directly."""
    repo_root = Path(__file__).resolve().parents[2]
    roots = (repo_root / "app" / "routes", repo_root / "app" / "services")

    violations: list[str] = []
    for root in roots:
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not isinstance(func, ast.Attribute):
                    continue
                if func.attr not in {"commit", "rollback"}:
                    continue
                violations.append(f"{path.relative_to(repo_root)}:{node.lineno}")

    assert not violations, (
        "Explicit commit()/rollback() calls found in request-bounded code:\n"
        + "\n".join(sorted(violations))
    )

