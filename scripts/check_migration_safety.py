"""Diff-scoped migration-safety checker for Alembic revisions.

Failure this descends from
--------------------------
Incident #669. A release migration's non-concurrent ``CREATE INDEX`` on
``summer_league_play_by_play_events`` queued behind a ~96-minute Summer League ingestion
transaction. The deploy stalled in the lock queue, the web workers' connection pool
filled with requests that could not complete, and DB-backed public routes returned 500
until the chain was broken by hand. See ``docs/plans/programmatic-code-discipline.md``
§1.7 and ``docs/plans/summer-league-desk-simplification-spec.md`` §5a.

Deploy-time lock contention should degrade the *deploy* — a fast, retryable failure —
never production reads.

The rules
---------
Both halves are required, and that is the whole point of this checker:

1. **``op.create_index`` must pass ``postgresql_concurrently=True``.** A bare
   ``CREATE INDEX`` takes a ``SHARE`` lock for the duration of the build, which blocks
   writes and — behind a long transaction — parks the deploy in the lock queue.

2. **A concurrent index operation must sit inside
   ``op.get_context().autocommit_block()``.** This repo's ``alembic/env.py`` runs
   migrations inside ``context.begin_transaction()``, and PostgreSQL rejects
   ``CREATE INDEX CONCURRENTLY`` inside a transaction block. A checker that demanded
   rule 1 alone would convert a lock hazard into a *guaranteed* failed release —
   so demanding rule 2 alongside it is not pedantry, it is the difference between the
   rule helping and the rule breaking every deploy that obeys it. Existing precedent:
   ``e7c75f3063ec_add_summer_league_games_tip_datetime.py``.

3. **``alembic/env.py`` must keep setting a ``lock_timeout``**, so a migration that
   cannot get its lock fails fast instead of camping in the queue ahead of production
   traffic. Checked whenever that file is part of the changeset.

Boundary semantics worth remembering: statements inside an autocommit block commit
immediately, so a mid-migration failure leaves earlier statements applied. Keep
autocommit index builds in dedicated, idempotent migrations (``if_not_exists=True``, as
``2c78f642217c`` does).

Why diff-scoped
---------------
Thirty-six existing revisions build indexes non-concurrently. They have already run in
production and will never run there again, so retrofitting them buys nothing and an
absolute rule would be a permanent wall of noise — the failure mode
``check_file_size_ratchet.py`` was shaped to avoid. This checker therefore evaluates only
the revisions a changeset adds or edits, mirroring that ratchet's git plumbing.

Escape hatch
------------
Small and new tables do not have the lock problem. Exempt them with a justification
visible in review, anywhere in the offending statement or on the line above it::

    # discipline: migration-safety new table, empty at deploy time
    op.create_index("ix_widgets_name", "widgets", ["name"])

A bare marker with no reason is rejected.

Usage::

    python scripts/check_migration_safety.py                          # vs HEAD
    python scripts/check_migration_safety.py --against origin/main    # CI
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from pathlib import Path

from _discipline import line_has_reasoned_waiver

# The escape-hatch slug; syntax and the mandatory-reason rule live in _discipline.py.
_RULE = "migration-safety"

# Index DDL is the operation that takes a table-level lock for the length of the build.
_INDEX_OPERATIONS = frozenset({"create_index", "drop_index"})

# Only `create_index` is required to be concurrent. A `drop_index` is brief, and
# demanding CONCURRENTLY there would force an autocommit block around a cheap statement.
_MUST_BE_CONCURRENT = frozenset({"create_index"})

MIGRATIONS_DIR = "alembic/versions"
ENV_PATH = "alembic/env.py"


def _git(*args: str) -> str:
    """Run a git command and return stdout, raising on failure."""
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=True
    ).stdout


def _merge_base(ref: str) -> str:
    """Resolve the merge base of ``ref`` and HEAD, falling back to ``ref`` itself.

    Two-dot ``git diff <ref>`` compares the tip of ``ref`` with the working tree, so a
    base branch that has advanced drags unrelated revisions into the comparison.
    """
    try:
        return _git("merge-base", ref, "HEAD").strip() or ref
    except subprocess.CalledProcessError:
        return ref


def changed_paths(against: str) -> list[str]:
    """Return added/modified migration revisions plus ``env.py`` when touched."""
    base = _merge_base(against)
    raw = _git(
        "diff",
        "--name-status",
        "--diff-filter=AM",
        base,
        "--",
        MIGRATIONS_DIR,
        ENV_PATH,
    )

    paths: list[str] = []
    for line in raw.splitlines():
        fields = line.split("\t")
        if len(fields) < 2:
            continue
        path = fields[-1]
        if path.endswith(".py") and Path(path).is_file():
            paths.append(path)
    return paths


def _parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    """Map every node in ``tree`` to its parent node."""
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    return parents


def _operation_name(node: ast.Call) -> str | None:
    """Return the Alembic index-operation name this call invokes, if any.

    Matches ``op.create_index(...)`` and a bare ``create_index(...)`` alike; the module
    alias is not a semantic difference.
    """
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr in _INDEX_OPERATIONS:
        return func.attr
    if isinstance(func, ast.Name) and func.id in _INDEX_OPERATIONS:
        return func.id
    return None


def _is_concurrent(node: ast.Call) -> bool:
    """True if the call passes ``postgresql_concurrently=True``."""
    for keyword in node.keywords:
        if keyword.arg == "postgresql_concurrently":
            return not (
                isinstance(keyword.value, ast.Constant) and keyword.value.value is False
            )
    return False


def _in_autocommit_block(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    """True if ``node`` is lexically inside a ``with ...autocommit_block()`` statement."""
    current: ast.AST | None = node
    while current is not None:
        if isinstance(current, (ast.With, ast.AsyncWith)):
            for item in current.items:
                for sub in ast.walk(item.context_expr):
                    if (
                        isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr == "autocommit_block"
                    ):
                        return True
        current = parents.get(current)
    return False


def _waived(lines: list[str], node: ast.Call, parents: dict[ast.AST, ast.AST]) -> bool:
    """True if a justified waiver comment covers the statement containing ``node``."""
    statement: ast.AST | None = node
    while statement is not None and not isinstance(statement, ast.stmt):
        statement = parents.get(statement)

    if isinstance(statement, ast.stmt):
        first, last = statement.lineno, statement.end_lineno or statement.lineno
    else:  # pragma: no cover - a Call always sits under some statement
        first = last = node.lineno

    # `first - 1` lets the justification sit on the line above the statement.
    for candidate in range(first - 1, last + 1):
        if 1 <= candidate <= len(lines) and line_has_reasoned_waiver(
            lines[candidate - 1], _RULE
        ):
            return True
    return False


def _check_lock_timeout(path: Path, source: str) -> list[str]:
    """Return a finding if ``alembic/env.py`` no longer configures a ``lock_timeout``.

    Matched case-insensitively: the setting reaches PostgreSQL as lowercase
    ``set_config('lock_timeout', ...)``, but it is just as likely to be routed through
    an ``ALEMBIC_LOCK_TIMEOUT`` constant, and a case-sensitive substring test would
    report that perfectly good code as missing.
    """
    if "lock_timeout" in source.lower():
        return []
    return [
        f"{path}: no lock_timeout configured; a blocked migration will camp in the "
        "lock queue ahead of production traffic"
    ]


def find_violations(path: Path, source: str) -> list[str]:
    """Return formatted violation strings for one migration's source text."""
    if path.as_posix() == ENV_PATH:
        return _check_lock_timeout(path, source)

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
        operation = _operation_name(node)
        if operation is None:
            continue

        concurrent = _is_concurrent(node)

        if not concurrent and operation in _MUST_BE_CONCURRENT:
            if not _waived(lines, node, parents):
                violations.append(
                    f"{path}:{node.lineno}: {operation}() without "
                    "postgresql_concurrently=True holds a table lock for the whole "
                    "build"
                )
            continue

        # Rule 2 is not waivable by the small-table escape hatch: CONCURRENTLY inside a
        # transaction is not a lock trade-off, it is a release that cannot succeed.
        if concurrent and not _in_autocommit_block(node, parents):
            violations.append(
                f"{path}:{node.lineno}: concurrent {operation}() outside "
                "op.get_context().autocommit_block(); PostgreSQL rejects CONCURRENTLY "
                "inside a transaction block, so this release would fail"
            )

    return violations


def main(argv: list[str] | None = None) -> int:
    """Check the changeset's migrations; return 1 if any violation is found."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--against",
        default="HEAD",
        help="Git ref to compare against (CI passes the PR merge base).",
    )
    args = parser.parse_args(argv)

    try:
        paths = changed_paths(args.against)
    except subprocess.CalledProcessError as exc:  # pragma: no cover - CI plumbing
        sys.stderr.write(f"migration safety: git diff failed: {exc}\n")
        return 0

    violations: list[str] = []
    for path in paths:
        file = Path(path)
        violations.extend(
            find_violations(file, file.read_text(encoding="utf-8", errors="replace"))
        )

    if not violations:
        return 0

    sys.stderr.write(
        "\n".join(
            [
                "ERROR: migration safety "
                f"({len(violations)} finding(s) in changed revisions).",
                "",
                "Incident #669: a non-concurrent CREATE INDEX queued behind a long",
                "ingestion transaction, stalled the deploy, and 500ed public routes on",
                "pool exhaustion. See docs/plans/programmatic-code-discipline.md §1.7.",
                "",
                *sorted(violations),
                "",
                "Build indexes concurrently, inside an autocommit block:",
                "",
                "    with op.get_context().autocommit_block():",
                "        op.create_index(",
                "            NAME, TABLE, [...], if_not_exists=True,",
                "            postgresql_concurrently=True,",
                "        )",
                "",
                "Small or brand-new tables can opt out with a justification:",
                "",
                "    # discipline: migration-safety <reason>",
                "",
            ]
        )
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
