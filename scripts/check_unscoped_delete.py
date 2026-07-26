"""Pre-commit helper banning unscoped ``delete(Model)`` statements.

Failure this descends from
--------------------------
The Summer League metrics rebuild full-wipes ``SummerLeaguePlayerSeason``,
``SummerLeagueMetricContext`` and ``SummerLeagueMetricModel`` on every run
(``app/services/summer_league/metrics.py``), destroying the time axis and the auditable
model-fit history. That is the one standing violation of principle P2 — *retain history by
default* — in an otherwise longitudinal-first codebase, and "wipe clean and recompute" is the
anti-pattern it names. See ``docs/plans/north-star-architecture.md`` P2 and
``docs/plans/programmatic-code-discipline.md`` §1.1.

The rule
--------
A ``delete(Model)`` construct with no ``.where(...)`` narrowing it deletes every row in the
table. Flag it.

Deliberately *not* flagged:

* ``db.delete(instance)`` — the ORM instance delete, inherently scoped to one row.
* ``delete(Model).where(...)`` — including longer chains such as
  ``delete(Model).execution_options(...).where(...)``, and the two-statement form where a bare
  ``delete(Model)`` is assigned to a name that is later narrowed with ``.where(...)`` inside the
  same function.

Escape hatch
------------
Some full-table deletes are legitimate: test fixtures, explicit ``--replace-run`` correction
paths, seed and demo scripts. Mark those with a comment carrying a reason, anywhere in the
offending statement or on the line directly above it::

    # discipline: unscoped-delete seed script, table is demo-only
    await db.execute(delete(DemoRow))

A bare ``# discipline: unscoped-delete`` with no reason is rejected — the point is that
exceptions are visible and argued in review rather than silent.

Usage: ``python scripts/check_unscoped_delete.py <paths...>``
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Callable

from _discipline import line_has_reasoned_waiver


# Methods that narrow a delete construct to a subset of rows.
_SCOPING_METHODS = frozenset({"where", "filter", "filter_by"})

# The escape-hatch slug; syntax and the mandatory-reason rule live in _discipline.py.
_RULE = "unscoped-delete"


def _parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    """Map every node in ``tree`` to its parent node."""
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    return parents


def _is_scoped_by_chain(node: ast.Call, parents: dict[ast.AST, ast.AST]) -> bool:
    """Return True if ``node``'s result flows into a ``.where()``-style call.

    Walks up through attribute-access and call links so intermediate builder calls
    (``.execution_options(...)``) do not hide the narrowing that follows them.
    """
    current: ast.AST = node
    while True:
        parent = parents.get(current)
        if isinstance(parent, ast.Attribute):
            if parent.attr in _SCOPING_METHODS:
                return True
            current = parent
            continue
        # `f(...)` where our node is the thing being called: keep climbing the chain.
        if isinstance(parent, ast.Call) and parent.func is current:
            current = parent
            continue
        return False


def _find_ancestor(
    node: ast.AST,
    parents: dict[ast.AST, ast.AST],
    predicate: Callable[[ast.AST], bool],
    *,
    include_self: bool,
) -> ast.AST | None:
    """Walk parent links from ``node`` until ``predicate`` matches.

    ``include_self`` decides whether ``node`` itself is a candidate. Both callers below
    used to inline this loop with *different* check-order, which is exactly the kind of
    near-miss the next helper added here would copy from the wrong one.
    """
    current: ast.AST | None = node if include_self else parents.get(node)
    while current is not None:
        if predicate(current):
            return current
        current = parents.get(current)
    return None


def _is_scope(node: ast.AST) -> bool:
    """True for nodes that delimit a function/class/module scope."""
    return isinstance(
        node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)
    )


def _scoped_later_in_scope(node: ast.Call, parents: dict[ast.AST, ast.AST]) -> bool:
    """Handle ``stmt = delete(Model)`` narrowed by ``stmt = stmt.where(...)`` later.

    Only considers the function (or module) the assignment lives in, so an unrelated
    same-named variable elsewhere cannot launder a real full-table delete.
    """
    assignment = parents.get(node)
    if not isinstance(assignment, ast.Assign) or len(assignment.targets) != 1:
        return False
    target = assignment.targets[0]
    if not isinstance(target, ast.Name):
        return False

    scope = _find_ancestor(assignment, parents, _is_scope, include_self=False)
    if scope is None:
        return False

    for candidate in ast.walk(scope):
        if (
            isinstance(candidate, ast.Attribute)
            and candidate.attr in _SCOPING_METHODS
            and isinstance(candidate.value, ast.Name)
            and candidate.value.id == target.id
        ):
            return True
    return False


def _waived(lines: list[str], node: ast.Call, parents: dict[ast.AST, ast.AST]) -> bool:
    """Return True if a justified waiver comment covers ``node``.

    Scans the line above the enclosing statement plus every line the statement spans,
    rather than just the call's own line. ``ruff format`` is free to rewrap a long
    ``await db.execute(delete(X))  # discipline: ...`` across several lines, which would
    otherwise orphan the waiver from the call it justifies and silently re-fail the file.
    """
    statement = _find_ancestor(
        node, parents, lambda n: isinstance(n, ast.stmt), include_self=True
    )
    if isinstance(statement, ast.stmt):
        first, last = statement.lineno, statement.end_lineno or statement.lineno
    else:
        first = last = node.lineno

    # `first - 1` lets the justification sit on the line above the statement.
    for candidate in range(first - 1, last + 1):
        if 1 <= candidate <= len(lines) and line_has_reasoned_waiver(
            lines[candidate - 1], _RULE
        ):
            return True
    return False


def find_violations(path: Path, source: str) -> list[str]:
    """Return formatted violation strings for one file's source text."""
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:  # pragma: no cover - pre-commit runs ruff first
        return [f"{path}:{exc.lineno}: could not parse ({exc.msg})"]

    parents = _parent_map(tree)
    lines = source.splitlines()
    violations: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # Only the SQLAlchemy `delete(Model)` construct. `db.delete(obj)` is an
        # instance delete and is inherently scoped.
        if not (isinstance(node.func, ast.Name) and node.func.id == "delete"):
            continue
        if not node.args:
            continue
        if _is_scoped_by_chain(node, parents):
            continue
        if _scoped_later_in_scope(node, parents):
            continue
        if _waived(lines, node, parents):
            continue

        target = ast.unparse(node.args[0])
        violations.append(f"{path}:{node.lineno}: delete({target}) has no .where(...)")

    return violations


def main(argv: list[str]) -> int:
    """Check each path given on the command line; return 1 if any violation is found."""
    paths = [Path(arg) for arg in argv[1:]]
    violations: list[str] = []
    for path in paths:
        if path.suffix != ".py" or not path.is_file():
            continue
        violations.extend(find_violations(path, path.read_text(encoding="utf-8")))

    if not violations:
        return 0

    sys.stderr.write(
        "\n".join(
            [
                "Unscoped delete(Model) deletes every row in the table, destroying history.",
                "This violates P2 (retain history by default) — see",
                "docs/plans/north-star-architecture.md.",
                "",
                "Prefer a dated version-flip publish: build the new version, then flip the",
                "current-version pointer. If the full delete is genuinely correct (test fixture,",
                "--replace-run correction path, seed script), justify it inline:",
                "",
                "    # discipline: unscoped-delete <reason>",
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
