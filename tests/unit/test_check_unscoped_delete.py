"""Tests for the unscoped-delete discipline checker.

The checker guards principle P2 (retain history by default) by flagging
``delete(Model)`` constructs with no ``.where(...)`` narrowing them. These tests pin both
halves of its contract: it must fire on a deliberately introduced violation (the Phase 0
exit criterion), and it must stay quiet on the scoped and waived forms so it does not train
anyone to bypass it.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from tests.unit._script_loader import SCRIPTS_DIR, load_script


checker = load_script("check_unscoped_delete")


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

    def test_conditionally_narrowed_delete_is_flagged(self):
        """A `.where()` reached only on some paths does not make the delete scoped.

        Caught in review of this checker. The builder form below was originally accepted,
        on the reasoning that a `.where()` against the same name proved narrowing. It
        proves no such thing — with `scope` falsy, `execute(stmt)` wipes the table. AST
        cannot establish that every path narrows, so the rule fails closed and legitimate
        builder code takes the documented escape hatch instead.
        """
        found = _violations(
            """
            async def rebuild(db, scope):
                stmt = delete(A)
                if scope:
                    stmt = stmt.where(A.id.in_(scope))
                await db.execute(stmt)
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


class TestRecognizesEveryImportSpelling:
    """An import alias is not a semantic difference, and must not be an escape route.

    ``app/services/admin_player_service.py`` imports ``delete as sa_delete`` and uses it at
    sixteen sites, so the aliased spelling is established house style here — an author
    copying it would otherwise have written code the checker could not see.
    """

    def test_aliased_import_is_flagged(self):
        """`from sqlalchemy import delete as sa_delete` then `sa_delete(Model)`."""
        found = _violations(
            """
            from sqlalchemy import delete as sa_delete

            async def rebuild(db):
                await db.execute(sa_delete(PlayerSeason))
            """
        )
        assert len(found) == 1
        assert "delete(PlayerSeason) has no .where(...)" in found[0]

    def test_module_qualified_call_is_flagged(self):
        """`import sqlalchemy as sa` then `sa.delete(Model)`."""
        found = _violations(
            """
            import sqlalchemy as sa

            async def rebuild(db):
                await db.execute(sa.delete(PlayerSeason))
            """
        )
        assert len(found) == 1

    def test_sqlmodel_import_is_flagged(self):
        """The construct is re-exported by sqlmodel, which this repo also imports from."""
        found = _violations(
            """
            from sqlmodel import delete as sm_delete

            async def rebuild(db):
                await db.execute(sm_delete(PlayerSeason))
            """
        )
        assert len(found) == 1

    def test_aliased_import_still_honours_scoping(self):
        """Alias resolution must not cost the checker its precision about `.where()`."""
        found = _violations(
            """
            from sqlalchemy import delete as sa_delete

            async def rebuild(db):
                await db.execute(sa_delete(A).where(A.id == 1))
            """
        )
        assert found == []

    def test_session_delete_is_not_confused_with_module_delete(self):
        """`db.delete(obj)` shares the attribute name but is an instance delete.

        Told apart by resolving which names are bound to SQLAlchemy modules — the reason
        the checker reads imports rather than pattern-matching the attribute name.
        """
        found = _violations(
            """
            import sqlalchemy as sa

            async def f(db, instance):
                await db.delete(instance)
            """
        )
        assert found == []

    def test_deep_module_path_is_flagged(self):
        """`import sqlalchemy` then `sqlalchemy.sql.delete(Model)`.

        The attribute chain is two levels deep; a checker that only resolves
        `Name.delete` receivers misses it.
        """
        found = _violations(
            """
            import sqlalchemy

            async def rebuild(db):
                await db.execute(sqlalchemy.sql.delete(PlayerSeason))
            """
        )
        assert len(found) == 1

    def test_aliased_submodule_import_is_flagged(self):
        """`import sqlalchemy.sql as sa_sql` then `sa_sql.delete(Model)`."""
        found = _violations(
            """
            import sqlalchemy.sql as sa_sql

            async def rebuild(db):
                await db.execute(sa_sql.delete(PlayerSeason))
            """
        )
        assert len(found) == 1

    def test_unrelated_deep_attribute_delete_is_not_flagged(self):
        """`client.admin.delete(url)` is somebody's HTTP verb, not the construct."""
        found = _violations(
            """
            import sqlalchemy

            async def f(client, url):
                await client.admin.delete(url)
            """
        )
        assert found == []


class TestLegacyQueryDelete:
    """`query(Model).delete()` is the canonical legacy bulk delete — same wipe, no import.

    No live usage in this async codebase, but it is what every old SQLAlchemy tutorial
    teaches, so it is exactly what a copy-paste would carry in.
    """

    def test_unfiltered_query_delete_is_flagged(self):
        """`db.query(Model).delete()` deletes every row."""
        found = _violations(
            """
            def wipe(db):
                db.query(PlayerSeason).delete()
            """
        )
        assert len(found) == 1
        assert "has no .filter(...)" in found[0]

    def test_filtered_query_delete_passes(self):
        """A `.filter(...)` in the chain scopes the delete."""
        found = _violations(
            """
            def trim(db, pid):
                db.query(PlayerSeason).filter(PlayerSeason.player_id == pid).delete()
            """
        )
        assert found == []

    def test_filter_by_in_the_chain_passes(self):
        """`.filter_by(...)` scopes it just as well."""
        found = _violations(
            """
            def trim(db, pid):
                db.query(PlayerSeason).filter_by(player_id=pid).delete()
            """
        )
        assert found == []

    def test_waiver_covers_query_delete(self):
        """The same escape hatch applies as for `delete(Model)`."""
        found = _violations(
            """
            def reset(db):
                # discipline: unscoped-delete test fixture, table is scratch
                db.query(PlayerSeason).delete()
            """
        )
        assert found == []

    def test_plain_method_named_delete_is_not_flagged(self):
        """`cache.delete()` and friends share the name, not the semantics."""
        found = _violations(
            """
            def evict(cache):
                cache.delete()
            """
        )
        assert found == []

    def test_stored_builder_is_flagged(self):
        """`q = db.query(Model)` then `q.delete()` is the same wipe, stored first."""
        found = _violations(
            """
            def wipe(db):
                q = db.query(PlayerSeason)
                q.delete()
            """
        )
        assert len(found) == 1

    def test_conditionally_filtered_builder_is_flagged(self):
        """A rebinding that only narrows on some paths must not launder the wipe.

        Same fail-closed stance as the `delete(Model)` builder form: proving the
        narrowing unconditional needs control-flow analysis, so the checker doesn't try.
        """
        found = _violations(
            """
            def wipe(db, scope):
                q = db.query(PlayerSeason)
                if scope:
                    q = q.filter(PlayerSeason.player_id.in_(scope))
                q.delete()
            """
        )
        assert len(found) == 1

    def test_builder_assigned_from_filtered_chain_passes(self):
        """A builder born scoped (`db.query(A).filter(...)`) is not the construct."""
        found = _violations(
            """
            def trim(db, pid):
                q = db.query(PlayerSeason).filter(PlayerSeason.player_id == pid)
                q.delete()
            """
        )
        assert found == []

    def test_waiver_covers_stored_builder(self):
        """The escape hatch works at the delete site, where the argument is reviewed."""
        found = _violations(
            """
            def reset(db):
                q = db.query(PlayerSeason)
                # discipline: unscoped-delete test fixture, table is scratch
                q.delete()
            """
        )
        assert found == []


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
        root = SCRIPTS_DIR.parent
        found: list[str] = []
        for directory in ("app", "scripts"):
            for path in sorted((root / directory).rglob("*.py")):
                found.extend(
                    checker.find_violations(path, path.read_text(encoding="utf-8"))
                )
        assert found == [], "\n".join(found)
