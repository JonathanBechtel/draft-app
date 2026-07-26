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

The construct is recognized under every spelling the repo uses, because an import alias is
not a semantic difference::

    delete(Model)                                  # from sqlalchemy import delete
    sa_delete(Model)                               # ... import delete as sa_delete
    sa.delete(Model)                               # import sqlalchemy as sa

The aliased form is not hypothetical: ``app/services/admin_player_service.py`` imports
``delete as sa_delete`` and uses it at sixteen sites, so an author copying the established
house style would have written code this checker could not see.

Deliberately *not* flagged:

* ``db.delete(instance)`` — the ORM instance delete, inherently scoped to one row. Told
  apart from ``sa.delete(Model)`` by resolving which names are bound to SQLAlchemy modules,
  rather than by guessing from the attribute name.
* ``delete(Model).where(...)`` — including longer chains such as
  ``delete(Model).execution_options(...).where(...)``.
* Raw SQL — ``text("DELETE FROM ...")`` and ``conn.exec_driver_sql(...)``. Out of reach for
  an AST checker reading string contents, and a known gap rather than a safe case; the
  runtime guards in ``docs/plans/programmatic-code-discipline.md`` Tier 2 are what cover
  what Tier 1 cannot see.

Deliberately **flagged**, and this is the interesting case: the two-statement builder form,
where a bare ``delete(Model)`` is assigned to a name and narrowed later::

    stmt = delete(A)
    if scope:
        stmt = stmt.where(A.id.in_(scope))
    await db.execute(stmt)

An earlier version of this checker accepted that, reasoning that a ``.where()`` on the same
name proved the delete was narrowed. It does not: the narrowing here is *conditional*, so
``scope=False`` executes a full-table delete — exactly the destruction this rule exists to
prevent. Proving otherwise needs control-flow analysis, which is beyond an AST checker, so
the rule fails closed. Genuinely-safe builder code takes the escape hatch below, where the
argument is visible in review.

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

# Modules that export the `delete()` construct. Imports from these are what bind a name —
# whatever it is spelled — to the thing this rule is about.
_DELETE_MODULES = frozenset(
    {
        "sqlalchemy",
        "sqlalchemy.sql",
        "sqlalchemy.sql.expression",
        "sqlmodel",
    }
)

# `delete` under any of these names is the construct even without a visible import, so
# coverage never depends on the checker resolving every import shape.
_DEFAULT_DELETE_NAMES = frozenset({"delete"})

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


def _resolve_delete_names(tree: ast.AST) -> tuple[set[str], set[str]]:
    """Return the names that mean ``delete``, as ``(bare_names, module_aliases)``.

    ``bare_names`` are called directly (``sa_delete(Model)``); ``module_aliases`` are called
    through an attribute (``sa.delete(Model)``). Resolving module aliases from the imports
    is what lets ``sa.delete(Model)`` be flagged while ``db.delete(instance)`` — an ORM
    instance delete on a session object, not a module — is left alone.
    """
    bare: set[str] = set(_DEFAULT_DELETE_NAMES)
    modules: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in _DELETE_MODULES:
                    # `import sqlalchemy` binds "sqlalchemy"; `as sa` binds "sa".
                    modules.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module not in _DELETE_MODULES:
                continue
            for alias in node.names:
                if alias.name == "delete":
                    bare.add(alias.asname or alias.name)
                elif f"{node.module}.{alias.name}" in _DELETE_MODULES:
                    # `from sqlalchemy import sql` — the bound name is still a module
                    # that exports `delete`.
                    modules.add(alias.asname or alias.name)

    return bare, modules


def _is_delete_construct(
    node: ast.Call, bare_names: set[str], module_aliases: set[str]
) -> bool:
    """Return True if ``node`` calls the SQLAlchemy ``delete()`` construct."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in bare_names
    if isinstance(func, ast.Attribute) and func.attr == "delete":
        return isinstance(func.value, ast.Name) and func.value.id in module_aliases
    return False


def find_violations(path: Path, source: str) -> list[str]:
    """Return formatted violation strings for one file's source text."""
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:  # pragma: no cover - pre-commit runs ruff first
        return [f"{path}:{exc.lineno}: could not parse ({exc.msg})"]

    parents = _parent_map(tree)
    lines = source.splitlines()
    bare_names, module_aliases = _resolve_delete_names(tree)
    violations: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # Only the SQLAlchemy `delete(Model)` construct, under any of its import
        # spellings. `db.delete(obj)` is an instance delete and is inherently scoped.
        if not _is_delete_construct(node, bare_names, module_aliases):
            continue
        if not node.args:
            continue
        if _is_scoped_by_chain(node, parents):
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
