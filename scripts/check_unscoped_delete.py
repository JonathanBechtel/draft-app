"""Pre-commit helper banning unscoped ``delete(Model)`` statements.

Failure this descends from
--------------------------
The Summer League metrics rebuild full-wipes ``SummerLeagueDerivedAgg``,
``SummerLeagueMetricContext`` and ``SummerLeagueMetricModel`` on every run
(``app/services/sources/summer_league/metrics.py``), destroying the time axis and the auditable
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
    sqlalchemy.sql.delete(Model)                   # import sqlalchemy
    db.query(Model).delete()                       # legacy ORM bulk delete

The aliased form is not hypothetical: ``app/services/admin_player_service.py`` imports
``delete as sa_delete`` and uses it at sixteen sites, so an author copying the established
house style would have written code this checker could not see. The ``query(...).delete()``
form has no live usage in this async codebase, but it is the canonical bulk-delete spelling
in every legacy SQLAlchemy tutorial — exactly what a copy-paste would carry in.

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

The stored query builder gets the same treatment: once ``q = session.query(Model)`` binds an
unscoped chain, ``q.delete()`` is flagged even if a ``q = q.filter(...)`` rebinding sits in
between — that narrowing may equally be conditional, and the waiver is the honest way out.

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


def _resolve_delete_names(tree: ast.AST) -> tuple[set[str], dict[str, str]]:
    """Return the names that mean ``delete``, as ``(bare_names, alias_to_module)``.

    ``bare_names`` are called directly (``sa_delete(Model)``); ``alias_to_module`` maps a
    bound name to the module path it stands for (``{"sa": "sqlalchemy"}``), so both
    ``sa.delete(Model)`` and the deeper ``sqlalchemy.sql.delete(Model)`` resolve. Resolving
    aliases from the imports is what lets those be flagged while ``db.delete(instance)`` —
    an ORM instance delete on a session object, not a module — is left alone.
    """
    bare: set[str] = set(_DEFAULT_DELETE_NAMES)
    modules: dict[str, str] = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            _collect_plain_imports(node, modules)
        elif isinstance(node, ast.ImportFrom):
            _collect_from_imports(node, bare, modules)

    return bare, modules


def _collect_plain_imports(node: ast.Import, modules: dict[str, str]) -> None:
    """Record module bindings from ``import ...`` statements into ``modules``."""
    for alias in node.names:
        root = alias.name.split(".")[0]
        if alias.name in _DELETE_MODULES:
            if alias.asname:
                # `import sqlalchemy.sql as sa_sql` binds the full path.
                modules[alias.asname] = alias.name
            else:
                # `import sqlalchemy.sql` binds the root package name.
                modules[root] = root
        elif root in _DELETE_MODULES and not alias.asname:
            # `import sqlalchemy.orm` still binds "sqlalchemy" itself.
            modules[root] = root


def _collect_from_imports(
    node: ast.ImportFrom, bare: set[str], modules: dict[str, str]
) -> None:
    """Record delete names and module bindings from ``from ... import ...``."""
    if node.module not in _DELETE_MODULES:
        return
    for alias in node.names:
        if alias.name == "delete":
            bare.add(alias.asname or alias.name)
        elif f"{node.module}.{alias.name}" in _DELETE_MODULES:
            # `from sqlalchemy import sql` — the bound name is still a module
            # that exports `delete`.
            modules[alias.asname or alias.name] = f"{node.module}.{alias.name}"


def _dotted_receiver(node: ast.expr) -> str | None:
    """Return ``node`` as a dotted name string, or None if it is not a plain chain."""
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return ".".join(reversed(parts))


def _is_delete_construct(
    node: ast.Call, bare_names: set[str], alias_to_module: dict[str, str]
) -> bool:
    """Return True if ``node`` calls the SQLAlchemy ``delete()`` construct."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in bare_names
    if isinstance(func, ast.Attribute) and func.attr == "delete":
        dotted = _dotted_receiver(func.value)
        if dotted is None:
            return False
        head, _, rest = dotted.partition(".")
        module = alias_to_module.get(head)
        if module is None:
            return False
        full = f"{module}.{rest}" if rest else module
        return full in _DELETE_MODULES
    return False


def _is_query_chain_unscoped(expr: ast.expr) -> bool:
    """Return True if ``expr`` is a call chain bottoming at ``query(...)`` with no filter.

    Walking the chain, a ``.filter``/``.filter_by``/``.where`` link scopes it; a chain
    reaching ``.query(...)`` without one does not.
    """
    current = expr
    while isinstance(current, ast.Call):
        inner = current.func
        if isinstance(inner, ast.Attribute):
            if inner.attr in _SCOPING_METHODS:
                return False
            if inner.attr == "query":
                return True
            current = inner.value
        elif isinstance(inner, ast.Name):
            return inner.id == "query"
        else:
            return False
    return False


_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)


def _enclosing_scope(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.AST | None:
    """Return the nearest enclosing function of ``node``, or None for module level."""
    found = _find_ancestor(
        node, parents, lambda n: isinstance(n, _SCOPE_NODES), include_self=False
    )
    return found


def _unscoped_query_names_by_scope(
    tree: ast.AST, parents: dict[ast.AST, ast.AST]
) -> dict[ast.AST | None, set[str]]:
    """Map each lexical scope to the names it assigns an unscoped ``query(...)`` chain.

    ``q = session.query(Model)`` then ``q.delete()`` is the stored-builder spelling of the
    same wipe. Names are collected per enclosing function (module level under ``None``) so
    an unscoped ``q`` in one function cannot taint an unrelated, born-scoped ``q`` in
    another. Within a scope, a name is deliberately *not* cleared by a later
    ``q = q.filter(...)`` rebinding: the narrowing may be conditional, and proving
    otherwise needs control-flow analysis this checker does not do — the same fail-closed
    stance the ``delete(Model)`` builder form takes. Genuinely-safe builder code carries
    the waiver.
    """
    by_scope: dict[ast.AST | None, set[str]] = {}
    for node in ast.walk(tree):
        targets: list[ast.expr]
        value: ast.expr | None
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        else:
            continue
        if value is not None and _is_query_chain_unscoped(value):
            scope = _enclosing_scope(node, parents)
            names = by_scope.setdefault(scope, set())
            names.update(t.id for t in targets if isinstance(t, ast.Name))
    return by_scope


def _name_is_unscoped_query(
    name: str,
    site: ast.AST,
    parents: dict[ast.AST, ast.AST],
    by_scope: dict[ast.AST | None, set[str]],
) -> bool:
    """Return True if ``name`` at ``site`` resolves to an unscoped query builder.

    Walks the scope chain outward (function → enclosing function → module) so a closure
    over an outer builder is still seen, while sibling functions stay isolated.
    """
    scope: ast.AST | None = _enclosing_scope(site, parents)
    while True:
        if name in by_scope.get(scope, ()):
            return True
        if scope is None:
            return False
        scope = _enclosing_scope(scope, parents)


def _is_unscoped_query_delete(
    node: ast.Call,
    parents: dict[ast.AST, ast.AST],
    by_scope: dict[ast.AST | None, set[str]],
) -> bool:
    """Return True for the legacy ``query(Model).delete()`` bulk delete with no filter.

    Covers both the direct chain (``db.query(Model).delete()``) and the stored builder
    (``q = db.query(Model)`` … ``q.delete()``) via the per-scope name map.
    """
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr == "delete"):
        return False
    if isinstance(func.value, ast.Name) and _name_is_unscoped_query(
        func.value.id, node, parents, by_scope
    ):
        return True
    return _is_query_chain_unscoped(func.value)


def find_violations(path: Path, source: str) -> list[str]:
    """Return formatted violation strings for one file's source text."""
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:  # pragma: no cover - pre-commit runs ruff first
        return [f"{path}:{exc.lineno}: could not parse ({exc.msg})"]

    parents = _parent_map(tree)
    lines = source.splitlines()
    bare_names, module_aliases = _resolve_delete_names(tree)
    query_names_by_scope = _unscoped_query_names_by_scope(tree, parents)
    violations: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        # Legacy ORM bulk delete: query(Model).delete() with no filter, whether
        # chained directly or stored in a builder name first.
        if _is_unscoped_query_delete(node, parents, query_names_by_scope):
            if not _waived(lines, node, parents):
                violations.append(
                    f"{path}:{node.lineno}: "
                    f"{ast.unparse(node.func)}() has no .filter(...)"
                )
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
