"""Capture the SQL statements a single in-process request issues.

Reuses the same SQLAlchemy ``after_cursor_execute`` primitive as
``scripts/explain_route.py`` (the per-page query diagnostic), but packaged as a
context manager that simply *counts* the meaningful statements a request fires.

Query count is a deterministic, data-volume-independent signal: unlike timing
(which is noise on local/CI hardware and does not reproduce prod), the number of
round-trips a page makes is the same regardless of row counts. It is the single
best early warning for the regression class that has actually bitten this app:
an N+1 loop or a new serial query bolted onto an already-long waterfall.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

# Transaction-control and session-setup statements that every request emits as
# bookkeeping. They are not "work" the page does, so they do not count against a
# route's budget (and `SET search_path` in particular is a test-harness artifact).
_NOISE_PREFIXES = (
    "BEGIN",
    "COMMIT",
    "ROLLBACK",
    "SAVEPOINT",
    "RELEASE",
    "SET",
    "SHOW",
    "DISCARD",
    "RESET",
)


def is_countable(statement: str) -> bool:
    """Return True if a statement counts as real query work for budgeting."""
    head = statement.lstrip().upper()
    return not head.startswith(_NOISE_PREFIXES)


@contextmanager
def count_queries(engine: AsyncEngine) -> Iterator[list[str]]:
    """Capture every countable SQL statement issued while the block is active.

    Args:
        engine: The async engine the app's sessions are bound to. The listener
            is attached to its underlying sync engine and removed on exit, so it
            never leaks into adjacent tests.

    Yields:
        A list that is appended to as statements execute. After the block exits
        the list holds every countable statement, in execution order.
    """
    captured: list[str] = []
    sync_engine = engine.sync_engine

    def _after(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        if is_countable(statement):
            captured.append(statement)

    event.listen(sync_engine, "after_cursor_execute", _after)
    try:
        yield captured
    finally:
        event.remove(sync_engine, "after_cursor_execute", _after)
