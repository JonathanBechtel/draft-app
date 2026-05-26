"""Run a page through the app in-process and EXPLAIN ANALYZE every SQL query it issued.

Usage:
    python scripts/explain_route.py /
    python scripts/explain_route.py /players/some-slug
    python scripts/explain_route.py / --top 5
    python scripts/explain_route.py / --all
    python scripts/explain_route.py / --no-plans   # timing table only, no EXPLAIN

Env:
    EXPLAIN_DATABASE_URL  Target DB URL for the request and EXPLAIN run. Falls back to
                          DATABASE_URL. Point this at a Neon prod read-only branch to
                          get realistic plans.

The script:
    1. Overrides DATABASE_URL with EXPLAIN_DATABASE_URL (if set) before importing the app.
    2. Registers a SQLAlchemy `before/after_cursor_execute` listener on the app's engine.
    3. Hits the route in-process via httpx.AsyncClient + ASGITransport.
    4. Runs `EXPLAIN (ANALYZE, BUFFERS)` on each captured SELECT/WITH statement.
    5. Prints a ranked report (slowest first), flagging Seq Scans and big row-estimate misses.

EXPLAIN ANALYZE executes the query. This is safe for SELECTs and is what we want, but do
not point this at a DB you cannot afford to read from with full row scans.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import event


@dataclass
class CapturedQuery:
    statement: str
    parameters: Any
    duration_ms: float
    rows: int | None


CAPTURED: list[CapturedQuery] = []


_NON_REPLAYABLE_PREFIXES = (
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

# Data-modifying keywords. Used to reject writable CTEs such as
# `WITH x AS (INSERT INTO ... RETURNING *) SELECT * FROM x`, which would execute
# their side effects when wrapped in EXPLAIN ANALYZE.
_DML_KEYWORDS_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|TRUNCATE)\b", re.IGNORECASE
)


def _is_replayable_select(statement: str) -> bool:
    head = statement.lstrip().upper()
    if head.startswith(_NON_REPLAYABLE_PREFIXES):
        return False
    if head.startswith("SELECT"):
        return True
    if head.startswith("WITH"):
        # WITH could be a read-only CTE or a writable CTE. EXPLAIN ANALYZE on the
        # latter would execute its writes against the target DB. Skip anything that
        # mentions a DML keyword anywhere in the statement.
        return _DML_KEYWORDS_RE.search(statement) is None
    return False


def _install_listeners(engine) -> None:
    sync_engine = engine.sync_engine

    @event.listens_for(sync_engine, "before_cursor_execute")
    def _before(conn, cursor, statement, parameters, context, executemany):
        context._explain_start = time.perf_counter()

    @event.listens_for(sync_engine, "after_cursor_execute")
    def _after(conn, cursor, statement, parameters, context, executemany):
        start = getattr(context, "_explain_start", None)
        if start is None:
            return
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        if not _is_replayable_select(statement):
            return
        try:
            rows = (
                cursor.rowcount
                if cursor.rowcount is not None and cursor.rowcount >= 0
                else None
            )
        except Exception:
            rows = None
        CAPTURED.append(
            CapturedQuery(
                statement=statement,
                parameters=parameters,
                duration_ms=elapsed_ms,
                rows=rows,
            )
        )


async def _run_explain(engine, query: CapturedQuery) -> str:
    explain_sql = "EXPLAIN (ANALYZE, BUFFERS, VERBOSE) " + query.statement
    async with engine.connect() as conn:
        result = await conn.exec_driver_sql(explain_sql, query.parameters)
        rows = result.fetchall()
    return "\n".join(row[0] for row in rows)


_SEQ_SCAN_RE = re.compile(r"Seq Scan on (\S+)")
_ROWS_MISMATCH_RE = re.compile(r"rows=(\d+).*?actual.*?rows=(\d+)")


def _summarize_plan(plan: str) -> list[str]:
    flags: list[str] = []
    seq_scans = sorted(set(_SEQ_SCAN_RE.findall(plan)))
    if seq_scans:
        flags.append(f"Seq Scan on: {', '.join(seq_scans)}")

    big_misses: list[str] = []
    for line in plan.splitlines():
        m = _ROWS_MISMATCH_RE.search(line)
        if not m:
            continue
        planned = int(m.group(1))
        actual = int(m.group(2))
        if planned == 0 or actual == 0:
            continue
        ratio = max(planned, actual) / max(min(planned, actual), 1)
        if ratio >= 10:
            big_misses.append(f"planned={planned} actual={actual}")
    if big_misses:
        flags.append("Row-estimate miss (>=10x): " + "; ".join(big_misses[:3]))

    if "Sort Method: external" in plan:
        flags.append("External sort (spilled to disk)")
    if "Heap Fetches:" in plan:
        for line in plan.splitlines():
            line = line.strip()
            if line.startswith("Heap Fetches:"):
                try:
                    n = int(line.split(":", 1)[1].strip().split()[0])
                    if n > 1000:
                        flags.append(f"High heap fetches: {n}")
                except ValueError:
                    pass
    return flags


def _truncate(text: str, limit: int) -> str:
    one_line = " ".join(text.split())
    if len(one_line) <= limit:
        return one_line
    return one_line[:limit] + "..."


def _print_report(
    path: str, status: int, total_ms: float, top: int, show_all: bool, plan_chars: int
) -> tuple[list[CapturedQuery], list[CapturedQuery]] | None:
    print()
    print(
        f"=== {path}  status={status}  total={total_ms:.0f}ms  queries={len(CAPTURED)} ==="
    )
    print()

    if not CAPTURED:
        print("No SELECT/WITH statements captured.")
        return None

    ranked = sorted(CAPTURED, key=lambda q: q.duration_ms, reverse=True)
    total_query_ms = sum(q.duration_ms for q in ranked)
    print(
        f"Total query time: {total_query_ms:.0f}ms ({total_query_ms / max(total_ms, 1) * 100:.0f}% of request)"
    )
    print()

    # Per-query summary table
    print(f"{'#':>3}  {'ms':>8}  {'rows':>6}  query")
    print("-" * 88)
    for i, q in enumerate(ranked, 1):
        rows_str = "-" if q.rows is None else str(q.rows)
        print(
            f"{i:>3}  {q.duration_ms:>8.1f}  {rows_str:>6}  {_truncate(q.statement, 70)}"
        )
    print()

    targets = ranked if show_all else ranked[:top]
    return ranked, targets


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("path", help="URL path to render, e.g. / or /players/some-slug")
    parser.add_argument(
        "--top",
        type=int,
        default=10,
        help="Show plans for the N slowest queries (default 10)",
    )
    parser.add_argument(
        "--all", action="store_true", help="Show plans for every captured query"
    )
    parser.add_argument(
        "--no-plans",
        action="store_true",
        help="Skip EXPLAIN; just show the timing table",
    )
    parser.add_argument(
        "--plan-chars",
        type=int,
        default=0,
        help="Truncate each plan to N chars (0 = no truncation)",
    )
    args = parser.parse_args()

    explain_db_url = os.environ.get("EXPLAIN_DATABASE_URL")
    if explain_db_url:
        os.environ["DATABASE_URL"] = explain_db_url
        print("Using EXPLAIN_DATABASE_URL for this run.")
    else:
        print("EXPLAIN_DATABASE_URL not set; using DATABASE_URL from env.")

    # Don't run init_db / create_all against the target.
    os.environ.setdefault("AUTO_INIT_DB", "false")

    # Import after env is configured so settings pick up the right URL.
    from httpx import ASGITransport, AsyncClient

    from app.main import app
    from app.utils.db_async import engine

    _install_listeners(engine)

    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://explain") as client:
        start = time.perf_counter()
        response = await client.get(args.path)
        total_ms = (time.perf_counter() - start) * 1000.0

    result = _print_report(
        args.path, response.status_code, total_ms, args.top, args.all, args.plan_chars
    )
    if result is None:
        await engine.dispose()
        return 0
    ranked, targets = result

    if args.no_plans:
        await engine.dispose()
        return 0

    for i, q in enumerate(targets, 1):
        rank = ranked.index(q) + 1
        print("=" * 88)
        print(f"#{rank}  {q.duration_ms:.1f}ms  rows={q.rows}")
        print("-" * 88)
        print(_truncate(q.statement, 400))
        print("-" * 88)
        try:
            plan = await _run_explain(engine, q)
        except Exception as exc:
            print(f"<EXPLAIN failed: {exc!r}>")
            print()
            continue
        flags = _summarize_plan(plan)
        if flags:
            print("FLAGS:")
            for f in flags:
                print(f"  - {f}")
            print()
        if args.plan_chars and len(plan) > args.plan_chars:
            plan = plan[: args.plan_chars] + "\n... [truncated]"
        print(plan)
        print()

    await engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
