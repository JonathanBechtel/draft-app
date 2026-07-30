"""Regression coverage for the two deploy-lock invariants in ``alembic/env.py``.

Failure this descends from
--------------------------
Incident #669. Two independent properties of the migration runner turned a slow Summer
League ingestion transaction into a 55-minute production outage, and both live in
``alembic/env.py`` rather than in any individual revision:

1. **No ``lock_timeout``** — the blocked ``CREATE INDEX`` sat in the lock queue instead
   of failing fast, so deploy-time contention became production downtime.
2. **One transaction for the whole revision chain** — four of the five pending revisions
   altered ``summer_league_environment_profiles``, taking ACCESS EXCLUSIVE on a table
   public routes read (``app/routes/summer_league.py:582,631``). Those locks were then
   held for the entire 55 minutes the fifth revision spent blocked, because nothing had
   committed yet.

Both are one-line settings, which is exactly why they need a test: nothing about
removing either one looks dangerous in review.

These assertions read the source rather than importing it — importing ``alembic/env.py``
executes migrations as a side effect.
"""

from __future__ import annotations

import ast
from pathlib import Path
import re

import pytest


ENV_PATH = Path(__file__).parents[2] / "alembic/env.py"


@pytest.fixture(scope="module")
def env_source() -> str:
    """Return the text of ``alembic/env.py``."""
    return ENV_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def env_tree(env_source: str) -> ast.Module:
    """Return the parsed AST of ``alembic/env.py``."""
    return ast.parse(env_source, filename=str(ENV_PATH))


def _context_configure_keywords(tree: ast.Module) -> dict[str, ast.expr]:
    """Return the keyword arguments passed to ``context.configure(...)`` online.

    ``do_run_migrations`` is the online path — the one Fly's release command runs. The
    offline path emits SQL and takes no locks.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "do_run_migrations":
            continue
        for call in ast.walk(node):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "configure"
            ):
                return {kw.arg: kw.value for kw in call.keywords if kw.arg}
    raise AssertionError("context.configure(...) not found in do_run_migrations")


def test_migrations_run_one_transaction_per_revision(env_tree: ast.Module) -> None:
    """Each revision commits on its own so its locks are not held chain-wide.

    Without this, a single blocked revision holds every earlier revision's ACCESS
    EXCLUSIVE locks for the duration of the block — the mechanism that took public
    Summer League routes down in #669.
    """
    keywords = _context_configure_keywords(env_tree)

    assert "transaction_per_migration" in keywords, (
        "alembic/env.py must set transaction_per_migration=True; without it every "
        "pending revision's locks are held until the last one commits"
    )
    value = keywords["transaction_per_migration"]
    assert isinstance(value, ast.Constant) and value.value is True


def test_a_lock_timeout_is_actually_executed(env_tree: ast.Module) -> None:
    """A migration that cannot get its lock fails the deploy, not production.

    Asserts the setting is *sent to PostgreSQL*, not merely mentioned. ``lock_timeout``
    appears in this file's comments and in an ``ALEMBIC_LOCK_TIMEOUT`` constant, so a
    substring check would still pass after someone deleted the call that applies it --
    which is exactly how this protection would realistically be lost.
    """
    executed_sql = [
        argument.value
        for node in ast.walk(env_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, (ast.Attribute, ast.Name))
        for argument in node.args
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
    ]

    assert any(
        re.search(
            r"set_config\s*\(\s*['\"]lock_timeout['\"]\s*,[^,]+,\s*false\s*\)",
            sql,
            re.IGNORECASE,
        )
        for sql in executed_sql
    ), (
        "alembic/env.py must execute set_config('lock_timeout', ..., false); "
        "a transaction-local setting would be discarded before migrations run"
    )


def test_the_lock_timeout_is_short_and_overridable(env_source: str) -> None:
    """The default must be seconds, not minutes — long enough is already too long.

    Parsed from the source's default rather than imported, and asserted loosely: the
    point is that the value is a short bounded duration, not a specific number.
    """
    tree = ast.parse(env_source)
    default: str | None = None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "getenv"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and "LOCK_TIMEOUT" in str(node.args[0].value)
        ):
            if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                default = str(node.args[1].value)

    assert default is not None, "the lock timeout should be env-overridable"
    assert default.endswith("s"), (
        f"expected a seconds-denominated default, got {default!r}"
    )
    assert 0 < int(default.removesuffix("s")) <= 60
