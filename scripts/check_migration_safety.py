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

4. **The migration graph must have one head.** Multiple heads make ``upgrade head``
   and future autogeneration ambiguous; join them with an explicit Alembic merge
   revision before shipping.

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
the revisions a changeset adds or edits, mirroring that ratchet's git plumbing. The
duplicate-ID and single-head checks are intentionally whole-tree checks because neither
property can be established from only one changed migration file.

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
import re
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from _discipline import line_has_reasoned_waiver

# The escape-hatch slug; syntax and the mandatory-reason rule live in _discipline.py.
_RULE = "migration-safety"

# Index DDL is the operation that takes a table-level lock for the length of the build.
_INDEX_OPERATIONS = frozenset({"create_index", "drop_index"})

# Only `create_index` is required to be concurrent. A `drop_index` is brief, and
# demanding CONCURRENTLY there would force an autocommit block around a cheap statement.
_MUST_BE_CONCURRENT = frozenset({"create_index"})

# Raw SQL is a live bypass, not a hypothetical one: five existing revisions build
# indexes through `op.execute("CREATE INDEX ...")` rather than `op.create_index`
# (e.g. `bb20c6f83560`, `w2x3y4z5a6b7`). A checker that recognized only the Alembic
# operation would let a new migration copy the established house pattern straight past
# the guard and re-create the production lock failure.
_RAW_CREATE_INDEX = re.compile(r"\bCREATE\s+(?:UNIQUE\s+)?INDEX\b", re.IGNORECASE)
_RAW_DROP_INDEX = re.compile(r"\bDROP\s+INDEX\b", re.IGNORECASE)
_RAW_CONCURRENTLY = re.compile(r"\bCONCURRENTLY\b", re.IGNORECASE)

# Calls whose string arguments are SQL that actually executes. Restricting the raw-SQL
# scan to these -- rather than to every string literal in the file -- keeps prose out of
# it: several migration docstrings discuss `CREATE INDEX` precisely because of this rule,
# and flagging the documentation of a hazard as the hazard would be self-defeating.
_SQL_SINKS = frozenset({"execute", "exec_driver_sql", "text"})

# `lock_timeout` must be *executed*, not merely mentioned. The `set_config(...)` form
# must also pass `false` for `is_local`: `true` would make the setting transaction-local,
# so the commit before `transaction_per_migration` begins would silently discard it.
_LOCK_TIMEOUT_STATEMENT = re.compile(
    r"""set_config\s*\(\s*['"]lock_timeout['"]\s*,[^,]+,\s*false\s*\)|\bSET\s+(?:LOCAL\s+)?lock_timeout""",
    re.IGNORECASE,
)

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


def _waived(lines: list[str], node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    """True if a justified waiver comment covers the statement containing ``node``."""
    statement: ast.AST | None = node
    while statement is not None and not isinstance(statement, ast.stmt):
        statement = parents.get(statement)

    if isinstance(statement, ast.stmt):
        first, last = statement.lineno, statement.end_lineno or statement.lineno
    else:  # pragma: no cover - an expression always sits under some statement
        first = last = getattr(node, "lineno", 1)

    # Scan the statement's own lines, plus the whole contiguous comment block directly
    # above it -- not just the single line above.
    #
    # A one-line lookback fails the obvious way to write a justification:
    #
    #     # discipline: migration-safety single-row table (one fit in production);
    #     # a non-concurrent build here takes a lock measured in milliseconds.
    #     op.create_index(...)
    #
    # The marker is two lines up, so the waiver is ignored and the check fails with the
    # justification sitting right there in the diff. It fails closed, so nothing unsafe
    # slips through -- but an escape hatch that rejects its own documented shape teaches
    # people to disable the hook instead of arguing the exception, which is the outcome
    # every rule in this file is trying to avoid.
    candidates = list(range(first, last + 1))
    cursor = first - 1
    while cursor >= 1 and lines[cursor - 1].lstrip().startswith("#"):
        candidates.append(cursor)
        cursor -= 1

    return any(
        1 <= candidate <= len(lines)
        and line_has_reasoned_waiver(lines[candidate - 1], _RULE)
        for candidate in candidates
    )


def _string_literal(
    node: ast.AST, bindings: Mapping[str, str] | None = None
) -> str | None:
    """Return the text of a string literal, or None if ``node`` is not one.

    Adjacent literals are already merged by the parser, so the multi-line
    ``"CREATE INDEX ..." " ON tbl (col)"`` form arrives here as one constant. f-strings
    contribute their literal segments, which is enough to see the DDL verb even when
    the table or index name is interpolated.

    Simple local names are resolved from ``bindings`` so ``sql = "CREATE INDEX ..."``
    followed by ``op.execute(sql)`` cannot bypass the raw-SQL check.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and bindings is not None:
        return bindings.get(node.id)
    if isinstance(node, ast.JoinedStr):
        return " ".join(
            part.value
            for part in node.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        )
    if isinstance(node, ast.Call):
        func = node.func
        name = (
            func.attr
            if isinstance(func, ast.Attribute)
            else func.id
            if isinstance(func, ast.Name)
            else None
        )
        if name == "text" and node.args:
            return _string_literal(node.args[0], bindings)
    return None


class _ExecutedSqlVisitor(ast.NodeVisitor):
    """Resolve SQL passed to execution sinks, including simple local variables."""

    def __init__(self) -> None:
        self.bindings: list[dict[str, str]] = [{}]
        self.found: list[tuple[ast.AST, str]] = []

    def _visit_scope(self, body: list[ast.stmt]) -> None:
        self.bindings.append(self.bindings[-1].copy())
        for statement in body:
            self.visit(statement)
        self.bindings.pop()

    def visit_Assign(self, node: ast.Assign) -> None:
        sql = _string_literal(node.value, self.bindings[-1])
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            if sql is None:
                self.bindings[-1].pop(target.id, None)
            else:
                self.bindings[-1][target.id] = sql

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name) and node.value is not None:
            sql = _string_literal(node.value, self.bindings[-1])
            if sql is None:
                self.bindings[-1].pop(node.target.id, None)
            else:
                self.bindings[-1][node.target.id] = sql

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_scope(node.body)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_scope(node.body)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_scope(node.body)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._visit_scope([ast.Expr(value=node.body)])

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = (
            func.attr
            if isinstance(func, ast.Attribute)
            else func.id
            if isinstance(func, ast.Name)
            else None
        )
        if name in _SQL_SINKS:
            for argument in node.args:
                sql = _string_literal(argument, self.bindings[-1])
                if sql:
                    self.found.append((argument, sql))
            return
        self.generic_visit(node)


def _executed_sql(tree: ast.AST) -> list[tuple[ast.AST, str]]:
    """Return ``(node, sql)`` for every string handed to an executing call.

    Nested forms resolve too: ``op.execute(text("..."))`` reaches the literal through
    the inner ``text(...)`` sink. Simple local assignments are resolved in source order.
    """
    visitor = _ExecutedSqlVisitor()
    visitor.visit(tree)
    return visitor.found


def _check_lock_timeout(path: Path, source: str) -> list[str]:
    """Return a finding if ``alembic/env.py`` does not *execute* a ``lock_timeout`` set.

    Deliberately not a substring test. `lock_timeout` appears in this file's comments
    and in an `ALEMBIC_LOCK_TIMEOUT` constant, so "the text is present somewhere" is
    satisfied by a version that never sends the setting to PostgreSQL — deleting the
    `connection.execute(set_config(...))` call while leaving the constant and the
    explanatory comment intact would pass a substring check and silently restore the
    deploy-blocking behavior this rule exists to prevent.
    """
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:  # pragma: no cover - pre-commit runs ruff first
        return [f"{path}:{exc.lineno}: could not parse ({exc.msg})"]

    for _node, sql in _executed_sql(tree):
        if _LOCK_TIMEOUT_STATEMENT.search(sql):
            return []
    return [
        f"{path}: no executed lock_timeout statement; a blocked migration will camp "
        "in the lock queue ahead of production traffic"
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

    violations.extend(_raw_sql_violations(path, tree, parents, lines))
    return violations


def _raw_sql_violations(
    path: Path,
    tree: ast.AST,
    parents: dict[ast.AST, ast.AST],
    lines: list[str],
) -> list[str]:
    """Apply the same two rules to index DDL written as raw SQL.

    ``op.execute("CREATE INDEX ...")`` takes exactly the same lock as
    ``op.create_index``; only the checker's view of it differs. Five existing revisions
    already use this spelling, so leaving it unhandled would mean the established house
    pattern is also the one that bypasses the guard.
    """
    violations: list[str] = []

    for node, sql in _executed_sql(tree):
        creates = bool(_RAW_CREATE_INDEX.search(sql))
        drops = bool(_RAW_DROP_INDEX.search(sql))
        if not (creates or drops):
            continue

        concurrent = bool(_RAW_CONCURRENTLY.search(sql))
        lineno = getattr(node, "lineno", 0)

        if creates and not concurrent:
            if not _waived(lines, node, parents):
                violations.append(
                    f"{path}:{lineno}: raw SQL CREATE INDEX without CONCURRENTLY "
                    "holds a table lock for the whole build"
                )
            continue

        if concurrent and not _in_autocommit_block(node, parents):
            violations.append(
                f"{path}:{lineno}: raw SQL CONCURRENTLY outside "
                "op.get_context().autocommit_block(); PostgreSQL rejects CONCURRENTLY "
                "inside a transaction block, so this release would fail"
            )

    return violations


_REVISION_RE = re.compile(r'^revision(?::\s*[^=]+)?\s*=\s*["\'](.+?)["\']', re.M)

# Where revisions live. Unlike the lock rules above, the duplicate check is
# whole-tree rather than diff-scoped: a collision is a property of the *pair*,
# and the changeset only ever contains one half of it.
_VERSIONS_DIR = Path(__file__).resolve().parent.parent / "alembic" / "versions"


def _literal_assignment(tree: ast.Module, name: str) -> object | None:
    """Return a module-level literal assignment, if one exists."""
    for node in tree.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets = [
                target for target in node.targets if isinstance(target, ast.expr)
            ]
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        if any(
            isinstance(target, ast.Name) and target.id == name for target in targets
        ):
            if value is None:
                return None
            try:
                return ast.literal_eval(value)
            except (ValueError, TypeError):
                return None
    return None


def _revision_metadata(path: Path) -> tuple[str | None, tuple[str, ...]]:
    """Read a revision ID and literal parent IDs without importing a migration."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return None, ()

    revision = _literal_assignment(tree, "revision")
    down_revision = _literal_assignment(tree, "down_revision")
    if not isinstance(revision, str):
        return None, ()
    if down_revision is None:
        return revision, ()
    if isinstance(down_revision, str):
        return revision, (down_revision,)
    if isinstance(down_revision, (tuple, list)) and all(
        isinstance(parent, str) for parent in down_revision
    ):
        return revision, tuple(down_revision)
    return revision, ()


def migration_heads() -> list[str]:
    """Return revision IDs that are not parents of another migration."""
    revisions: set[str] = set()
    parents: set[str] = set()
    for path in sorted(_VERSIONS_DIR.glob("*.py")):
        revision, down_revisions = _revision_metadata(path)
        if revision is None:
            continue
        revisions.add(revision)
        parents.update(down_revisions)
    return sorted(revisions - parents)


def divergent_head_violations() -> list[str]:
    """Return a finding when the migration graph has more than one head."""
    heads = migration_heads()
    if len(heads) <= 1:
        return []
    return [
        "alembic/versions: migration tree has multiple heads "
        f"({', '.join(heads)}); add an Alembic merge revision before shipping"
    ]


def duplicate_revision_ids() -> list[str]:
    """Find revision IDs claimed by more than one migration file.

    Alembic cannot build an unambiguous revision map when two files declare the
    same ``revision``, so ``alembic upgrade heads`` -- this repo's production
    ``release_command`` -- fails outright. Nothing else catches it: the test
    fixtures build schemas with ``SQLModel.metadata.create_all`` rather than by
    running migrations, so a duplicate sails through a fully green test suite
    and only surfaces at deploy time.

    It happens when a revision ID is hand-written instead of generated by
    ``alembic revision``, which is routine for enum-label migrations that need
    no autogenerated body.

    Returns:
        One message per colliding ID, naming every file that claims it.
    """
    by_id: dict[str, list[str]] = {}
    for path in sorted(_VERSIONS_DIR.glob("*.py")):
        match = _REVISION_RE.search(path.read_text(encoding="utf-8", errors="replace"))
        if match is not None:
            by_id.setdefault(match.group(1), []).append(path.name)
    return [
        f"alembic/versions: revision id {rev!r} is claimed by "
        f"{len(files)} files: {', '.join(files)}"
        for rev, files in sorted(by_id.items())
        if len(files) > 1
    ]


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
    violations.extend(duplicate_revision_ids())
    violations.extend(divergent_head_violations())

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
