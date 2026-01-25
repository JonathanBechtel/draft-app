"""Pre-commit helper enforcing request-bounded transaction conventions.

This check intentionally only targets request-bounded code (routes/services).
CLI/scripts are allowed to use explicit commit()/rollback() for now.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path


_FORBIDDEN_ATTRS = {"commit", "rollback"}


def _find_violations(paths: list[Path]) -> list[str]:
    violations: list[str] = []
    for path in paths:
        if path.suffix != ".py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute):
                continue
            if func.attr not in _FORBIDDEN_ATTRS:
                continue
            violations.append(f"{path}:{node.lineno}")
    return violations


def main(argv: list[str]) -> int:
    paths = [Path(arg) for arg in argv[1:]]
    if not paths:
        return 0

    violations = _find_violations(paths)
    if not violations:
        return 0

    sys.stderr.write(
        "\n".join(
            [
                "Explicit commit()/rollback() calls are forbidden in request-bounded"
                " code (routes/services). Use `async with db.begin(): ...` instead.",
                "",
                "Violations:",
                *sorted(violations),
                "",
            ]
        )
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
