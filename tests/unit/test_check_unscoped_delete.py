"""Tests for the unscoped-delete discipline checker.

The checker guards principle P2 (retain history by default) by flagging
``delete(Model)`` constructs with no ``.where(...)`` narrowing them. These tests pin both
halves of its contract: it must fire on a deliberately introduced violation (the Phase 0
exit criterion), and it must stay quiet on the scoped and waived forms so it does not train
anyone to bypass it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from textwrap import dedent

import pytest


_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_unscoped_delete.py"


def _load_checker():
    """Import the checker script by path (scripts/ is not an installed package)."""
    spec = importlib.util.spec_from_file_location("check_unscoped_delete", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


def _violations(source: str) -> list[str]:
    return checker.find_violations(Path("sample.py"), dedent(source))


class TestFlagsUnscopedDeletes:
    """Sources that destroy whole tables must be reported."""

    def test_bare_delete_is_flagged(self):
        """The founding failure shape: delete(Model) with nothing narrowing it."""
        found = _violations(
            """
            async def rebuild(db):
                await db.execute(delete(PlayerSeason))
            """
        )
        assert len(found) == 1
        assert "delete(PlayerSeason) has no .where(...)" in found[0]

    def test_each_offending_line_is_reported_separately(self):
        """A three-table wipe should surface as three actionable line references."""
        found = _violations(
            """
            async def rebuild(db):
                await db.execute(delete(A))
                await db.execute(delete(B))
                await db.execute(delete(C))
            """
        )
        assert len(found) == 3
        assert [f.split(":")[1] for f in found] == ["3", "4", "5"]

    def test_waiver_without_a_reason_is_rejected(self):
        """A bare marker must not silence the check — exceptions have to be argued."""
        found = _violations(
            """
            async def rebuild(db):
                await db.execute(delete(A))  # discipline: unscoped-delete
            """
        )
        assert len(found) == 1

    def test_unrelated_variable_where_does_not_launder_a_wipe(self):
        """A `.where()` on a different name must not be credited to this delete."""
        found = _violations(
            """
            async def rebuild(db):
                other = select(A).where(A.id == 1)
                stmt = delete(A)
                await db.execute(stmt)
            """
        )
        assert len(found) == 1


class TestAllowsScopedAndWaivedDeletes:
    """Legitimate forms must stay silent, or the rule trains people to bypass it."""

    @pytest.mark.parametrize(
        "body",
        [
            "await db.execute(delete(A).where(A.id == 1))",
            "await db.execute(delete(A).filter(A.id == 1))",
            "await db.execute(delete(A).execution_options(x=1).where(A.id == 1))",
            "db.delete(instance)",
            "await db.execute(delete(A))  # discipline: unscoped-delete seed script",
        ],
        ids=["where", "filter", "builder-chain", "orm-instance", "waived-inline"],
    )
    def test_scoped_forms_pass(self, body: str):
        """Narrowed deletes, instance deletes and justified waivers are all fine."""
        assert _violations(f"async def f(db, instance):\n    {body}\n") == []

    def test_waiver_on_the_line_above_counts(self):
        """Long call lines can carry their justification on the preceding line."""
        found = _violations(
            """
            async def rebuild(db):
                # discipline: unscoped-delete test fixture reset
                await db.execute(delete(A))
            """
        )
        assert found == []

    def test_delete_narrowed_by_a_later_statement_passes(self):
        """`stmt = delete(A)` then `stmt = stmt.where(...)` is scoped, just not inline."""
        found = _violations(
            """
            async def rebuild(db, scope):
                stmt = delete(A)
                if scope:
                    stmt = stmt.where(A.id.in_(scope))
                await db.execute(stmt)
            """
        )
        assert found == []

    def test_waiver_survives_formatter_rewrapping(self):
        """A trailing waiver must still count after ruff format splits the call.

        `await db.execute(delete(X))  # discipline: ...` exceeds the line budget, so the
        formatter rewraps it and the comment lands two lines below the delete() call. If the
        checker only looked at the call's own line the waiver would be silently orphaned.
        """
        found = _violations(
            """
            async def rebuild(db):
                await db.execute(
                    delete(A)
                )  # discipline: unscoped-delete correction path
            """
        )
        assert found == []

    def test_multiline_chain_passes(self):
        """Formatter-wrapped chains are the common real-world shape."""
        found = _violations(
            """
            async def rebuild(db):
                await db.execute(
                    delete(A).where(
                        A.id == 1,
                    )
                )
            """
        )
        assert found == []


class TestRepositoryIsClean:
    """The checker must pass over the tree it ships with."""

    def test_app_and_scripts_have_no_unwaived_violations(self):
        """Every remaining full-table delete carries a visible, justified waiver."""
        root = _SCRIPT.resolve().parents[1]
        found: list[str] = []
        for directory in ("app", "scripts"):
            for path in sorted((root / directory).rglob("*.py")):
                found.extend(
                    checker.find_violations(path, path.read_text(encoding="utf-8"))
                )
        assert found == [], "\n".join(found)
