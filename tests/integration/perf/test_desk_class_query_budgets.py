"""Per-class query-count budgets for the Desk latency-class partition (#699/#716).

#699's acceptance checklist included "Query budgets extended per class
(discipline §3.3)", but PR #706 shipped the latency-class partition without
it: only the composite ``DESK_TICK_DURATION_BUDGET_MS`` existed, so query-count
growth inside one class (e.g. the fast poller, whose entire reason for
existing is a small, predictable footprint) only failed CI if it happened to
also trip the old composite budget.

This module adds one query-count budget test per class --
:func:`~app.services.summer_league.desk_tick.fast.run_fast_tick`,
:func:`~app.services.summer_league.desk_tick.projection.run_projection_tick`,
and :func:`~app.services.summer_league.desk_tick.backbone.run_backbone_tick`
-- run against the same real-capture replay fixtures
(``tests/integration/_desk_replay.py``) the #699 acceptance suite
(``tests/integration/test_sl_desk_latency_classes.py``) already uses, so the
budgeted scenario is the same live-window shape that suite proves is
reliable under contention. Budgets count SQL statements, not wall-clock time
-- see ``tests/integration/perf/budgets.py``'s module docstring for the
"bump-the-number" protocol these follow.

Warm-up convention (#488): the generic route-budget harness renders once
untracked before capturing so one-time process-level cache fills don't count
against the budget. There is no known module-global cache in the desk-tick
code paths today, but each tick here is also idempotent (scoreboard/grade/
storyline/snapshot writes are all upserts, never insert-only), so the same
"run once untracked, then measure" shape doubles as steady-state
measurement: a tick's *second* run over the same frame is the query count a
real poller settles into once the narrow set of rows it touches already
exist, which is the common case after the very first tick of an event.
"""

from __future__ import annotations

from pathlib import Path
from typing import Awaitable, Callable, TypeVar

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.services.summer_league.desk_tick.backbone import (
    BackboneTickResult,
    run_backbone_tick,
)
from app.services.summer_league.desk_tick.fast import FastTickResult, run_fast_tick
from app.services.summer_league.desk_tick.projection import (
    ProjectionTickResult,
    run_projection_tick,
)
from app.services.summer_league.desk_tick.shared import (
    NO_WRITER_LOCK,
    TickContext,
    WriterLockPolicy,
)
from app.services.summer_league.nba_stats_client import NBAStatsClient
from tests.integration._desk_replay import (
    CAPTURE_REFERENCE_NOW,
    ReplaySession,
    real_live_window_frames,
)
from tests.integration.perf._capture import count_queries
from tests.integration.perf.budgets import (
    DESK_BACKBONE_TICK_QUERY_BUDGET,
    DESK_FAST_TICK_QUERY_BUDGET,
    DESK_PROJECTION_TICK_QUERY_BUDGET,
)
from tests.integration.test_sl_desk_latency_classes import _seed_live_window

pytestmark = pytest.mark.asyncio

_T = TypeVar("_T")


async def _steady_state_query_count(
    db: AsyncSession,
    engine: AsyncEngine,
    run_once: Callable[[], Awaitable[_T]],
) -> tuple[int, _T]:
    """Run ``run_once`` untracked, then again under capture, committing each time.

    Mirrors ``test_route_query_budgets.py``'s warm-up render: the first call
    is discarded so the measured count reflects the tick's steady state
    (every upsert target already exists) rather than whichever cold-start
    shape happened to run first -- deterministic regardless of worker/test
    ordering under xdist.

    Returns:
        A ``(query_count, measured_result)`` tuple. The result comes back so
        callers can assert the tick actually did its work -- see the liveness
        note on each test below.
    """
    await run_once()
    await db.commit()
    with count_queries(engine) as captured:
        result = await run_once()
    await db.commit()
    return len(captured), result


async def test_fast_tick_within_query_budget(
    db_session: AsyncSession,
    async_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    """The fast (live-poller) class must stay within its per-tick query budget.

    A failure here means a change added SQL round-trips to the class whose
    entire purpose is a small, predictable footprint that can never queue
    behind the backbone (#699 spec §2). Fix an accidental per-row query
    before raising the budget; raise it deliberately, in the same diff, only
    for a genuinely necessary new query.

    The liveness assertions below mirror ``test_route_query_budgets.py``'s
    ``status_code == 200`` check: a dormant tick early-returns in ~2 queries,
    which would satisfy the budget trivially and silently turn this guard
    into a no-op if the seeded window ever drifts out of the live phase.
    """
    await _seed_live_window(db_session)

    session = ReplaySession()
    client = NBAStatsClient(session=session)
    frame = real_live_window_frames(count=1)[0]
    session.use(frame)

    async def _run() -> FastTickResult:
        return await run_fast_tick(
            db_session,
            TickContext(
                now=frame.now,
                raw_root=tmp_path,
                client=client,
                lock=NO_WRITER_LOCK,
            ),
        )

    count, result = await _steady_state_query_count(db_session, async_engine, _run)
    assert result.dormant is False, (
        "fast tick went dormant, so this budget measured an early return, "
        "not the live-poller path it exists to guard."
    )
    assert result.scoreboard_report is not None
    assert count <= DESK_FAST_TICK_QUERY_BUDGET, (
        f"run_fast_tick issued {count} queries, over its budget of "
        f"{DESK_FAST_TICK_QUERY_BUDGET} (tests/integration/perf/budgets.py)."
    )


async def test_projection_tick_within_query_budget(
    db_session: AsyncSession,
    async_engine: AsyncEngine,
) -> None:
    """The projection (medium, hourly-rebuild) class must stay within budget.

    This class is a pure reader of canonical data plus a writer of the Desk
    projection tables it exclusively owns (grades, storylines, commentary,
    render snapshots) -- see #699 spec §2. It never scales with roster/game
    volume by design (#548); this budget catches a regression that
    reintroduces a per-player/per-slot query into any of those steps.
    """
    await _seed_live_window(db_session)

    async def _run() -> ProjectionTickResult:
        return await run_projection_tick(
            db_session,
            TickContext(now=CAPTURE_REFERENCE_NOW, lock=NO_WRITER_LOCK),
        )

    count, result = await _steady_state_query_count(db_session, async_engine, _run)
    assert result.dormant is False, (
        "projection tick went dormant, so this budget measured an early "
        "return, not the rebuild path it exists to guard."
    )
    assert count <= DESK_PROJECTION_TICK_QUERY_BUDGET, (
        f"run_projection_tick issued {count} queries, over its budget of "
        f"{DESK_PROJECTION_TICK_QUERY_BUDGET} (tests/integration/perf/budgets.py)."
    )


async def test_backbone_tick_within_query_budget(
    db_session: AsyncSession,
    async_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    """The backbone (slow, normalize + scoped-rebuild) class must stay within budget.

    Unlike the other two classes, production always runs this one behind the
    shared writer lock (``app/cli/sl_desk_backbone_tick.py``), so the budget
    is measured the same way -- with a real (uncontended, so immediately
    granted) lock acquisition counted as part of its query cost, not
    :data:`NO_WRITER_LOCK`.

    The perf dataset's competitions have no audited raw run on disk yet, so
    ``normalize_competition`` best-effort skips (the common no-new-raw-data
    case -- see its docstring) rather than reaching the metrics rebuild; this
    budgets that steady no-op-normalize path, which is the one every
    off-peak backbone tick without a fresh raw fetch actually takes.
    """
    await _seed_live_window(db_session)

    async def _run() -> BackboneTickResult:
        return await run_backbone_tick(
            db_session,
            TickContext(
                now=CAPTURE_REFERENCE_NOW,
                raw_root=tmp_path,
                lock=WriterLockPolicy(enabled=True),
            ),
        )

    count, result = await _steady_state_query_count(db_session, async_engine, _run)
    assert result.dormant is False, (
        "backbone tick went dormant, so this budget measured an early "
        "return, not the normalize path it exists to guard."
    )
    assert count <= DESK_BACKBONE_TICK_QUERY_BUDGET, (
        f"run_backbone_tick issued {count} queries, over its budget of "
        f"{DESK_BACKBONE_TICK_QUERY_BUDGET} (tests/integration/perf/budgets.py)."
    )
