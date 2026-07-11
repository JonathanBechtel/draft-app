"""Read-only readiness checker for the Summer League Desk cron (#536).

`docs/plans/summer-league-desk-launch-readiness.md` item 4 ("Desk deployment
readiness") calls for a gate a human (or CI step) can run before turning on
the Desk cron machine, and again right after its first manual tick, without
ever mutating the database. This script is that gate.

Two modes:

* ``preflight`` — run **before** creating/enabling the Desk scheduled machine
  (or before promoting a build to production). Confirms the two
  build-time prerequisites the tick itself hard-depends on:
  this year's Summer League competition(s) are registered
  (:func:`scripts.sl_desk_tick.resolve_target_competitions`'s fallback path
  reads exactly this), and Job A
  (``scripts/build_sl_cohort_baselines.py``) has produced an *active* T1
  baseline for every required grain -- event, debut, and game (the game
  grain was added by #525; ``run_desk_tick`` raises ``RuntimeError`` outright
  if no active baseline exists at all). State freshness and render snapshots
  are checked too, but leniently: neither is expected to exist before the
  first tick has ever run, so their absence is reported informationally
  (``skip``), not as a failure.
* ``post-tick`` — run **after** a deliberate, one-time manual tick
  (``scripts/sl_desk_tick.py``) to confirm it actually took effect. The same
  two build-time checks are re-verified (Job A must still be satisfied), plus
  two checks that only make sense once a tick has run: the tick's own step 0
  (`registry.sync_summer_league_event`) must have synced an active
  ``events`` row, and ``event_desk_state`` must carry a freshness stamp no
  older than ``--staleness-hours`` (default matches twice
  ``app.services.event_desk.controller.TICK_INTERVAL``, tolerating exactly
  one missed hourly tick before flagging staleness). Render snapshots remain
  a "when present" check in both modes -- materialization is a separate
  ticket's concern, so an event with none yet is not itself a readiness
  failure, but a mismatched ``schema_version`` on rows that *do* exist is.

**Never writes.** Every check is a plain ``SELECT`` over an
``AsyncSession`` opened without a transaction; nothing is ever ``add``ed,
``insert``ed, or committed. Safe to run against a live stage/prod database at
any time.

Exit code: ``0`` when every applicable check passes (or is skipped), ``1``
when any check fails -- each failing category prints its own distinct
message so a human (or CI log) can see exactly what's missing without
re-deriving it.

Run:
  scripts/with-db-env.sh conda run -n draftguru python scripts/check_sl_desk_readiness.py preflight
  scripts/with-db-env.sh conda run -n draftguru python scripts/check_sl_desk_readiness.py post-tick
  scripts/with-db-env.sh conda run -n draftguru python scripts/check_sl_desk_readiness.py post-tick --staleness-hours 3
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Literal, Optional, Sequence

from dotenv import load_dotenv
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
load_dotenv()

from app.schemas.event_desk import Event, EventDeskState  # noqa: E402
from app.schemas.event_desk_render_snapshot import EventDeskRenderSnapshot  # noqa: E402
from app.schemas.summer_league import SummerLeagueCompetition  # noqa: E402
from app.schemas.summer_league_desk import (  # noqa: E402
    SummerLeagueCohortBaseline,
    SummerLeagueDeskGrain,
)
from app.services.event_desk.render_snapshots import CURRENT_SCHEMA_VERSION  # noqa: E402
from app.services.event_desk.timeutils import to_eastern_date  # noqa: E402
from app.services.summer_league.scoreboard_ingest import (  # noqa: E402
    EVENT_KEY_SUMMER_LEAGUE,
)
from app.utils.db_async import SessionLocal, engine  # noqa: E402

ReadinessMode = Literal["preflight", "post-tick"]

# Job A (`scripts/build_sl_cohort_baselines.py`) must produce an active baseline for
# each of these grains before the hourly tick can grade *and* Ledger/streak commentary
# can read the game-grain distribution (#525) or debut distribution (#539) it needs.
REQUIRED_BASELINE_GRAINS: frozenset[SummerLeagueDeskGrain] = frozenset(
    {
        SummerLeagueDeskGrain.EVENT,
        SummerLeagueDeskGrain.DEBUT,
        SummerLeagueDeskGrain.GAME,
    }
)

# `app.services.event_desk.controller.TICK_INTERVAL` is one hour; tolerate exactly one
# missed scheduled tick before flagging freshness as stale.
DEFAULT_STALENESS_HOURS = 2.0


class ReadinessStatus(str, Enum):
    """One check's outcome."""

    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"


@dataclass(frozen=True)
class CheckResult:
    """One category's readiness verdict."""

    category: str
    status: ReadinessStatus
    message: str


@dataclass(frozen=True)
class ReadinessReport:
    """Every category's verdict for one ``build_readiness_report`` call."""

    mode: ReadinessMode
    now: datetime
    results: tuple[CheckResult, ...]

    @property
    def ok(self) -> bool:
        """Whether every category passed (skips do not count against readiness)."""
        return not any(r.status == ReadinessStatus.FAIL for r in self.results)


async def _check_registration(
    db: AsyncSession, *, mode: ReadinessMode, today_year: int, event_key: str
) -> CheckResult:
    """Category 1 -- this year's competition(s) registered, plus (post-tick) a synced events row."""
    comp_stmt = select(SummerLeagueCompetition).where(
        SummerLeagueCompetition.year == today_year  # type: ignore[arg-type]
    )
    competitions = (await db.execute(comp_stmt)).scalars().all()
    if not competitions:
        return CheckResult(
            category="registration",
            status=ReadinessStatus.FAIL,
            message=(
                f"No Summer League competitions configured for {today_year}; "
                "register this year's competition(s) before enabling the Desk cron."
            ),
        )

    if mode == "preflight":
        return CheckResult(
            category="registration",
            status=ReadinessStatus.PASS,
            message=f"{len(competitions)} competition(s) configured for {today_year}.",
        )

    # post-tick: `run_desk_tick`'s step-0 pre-check (`registry.sync_summer_league_event`)
    # unconditionally upserts an active `events` row on *every* tick, dormant or not --
    # confirm that actually happened rather than re-checking only the fallback path.
    event_stmt = select(Event).where(
        Event.key == event_key,  # type: ignore[arg-type]
        Event.is_active.is_(True),  # type: ignore[attr-defined]
    )
    event = (await db.execute(event_stmt)).scalar_one_or_none()
    if event is None:
        return CheckResult(
            category="registration",
            status=ReadinessStatus.FAIL,
            message=(
                f"No active 'events' row found for key={event_key!r} after a tick; "
                "Job B (scripts/sl_desk_tick.py) should have synced this on its first run."
            ),
        )
    competition_ids = event.calendar_ref.get("competition_ids")
    if not isinstance(competition_ids, list) or not competition_ids:
        return CheckResult(
            category="registration",
            status=ReadinessStatus.FAIL,
            message=(
                f"'events' row for key={event_key!r} has an empty "
                "calendar_ref['competition_ids']; registration did not sync correctly."
            ),
        )
    return CheckResult(
        category="registration",
        status=ReadinessStatus.PASS,
        message=(
            f"{len(competitions)} competition(s) configured for {today_year}; "
            f"events row synced with {len(competition_ids)} competition_id(s)."
        ),
    )


async def _check_baselines(db: AsyncSession) -> CheckResult:
    """Category 2 -- Job A produced an active baseline for every required grain."""
    stmt = select(SummerLeagueCohortBaseline.grain).where(  # type: ignore[call-overload]
        SummerLeagueCohortBaseline.is_active.is_(True)  # type: ignore[attr-defined]
    )
    present = set((await db.execute(stmt)).scalars().all())
    missing = REQUIRED_BASELINE_GRAINS - present
    if missing:
        missing_str = ", ".join(sorted(g.value for g in missing))
        return CheckResult(
            category="baselines",
            status=ReadinessStatus.FAIL,
            message=(
                f"Missing active Summer League cohort baseline grain(s): {missing_str}; "
                "run scripts/build_sl_cohort_baselines.py (Job A) before enabling the cron."
            ),
        )
    present_str = ", ".join(sorted(g.value for g in present))
    return CheckResult(
        category="baselines",
        status=ReadinessStatus.PASS,
        message=f"Active baseline grains present: {present_str}.",
    )


async def _check_freshness(
    db: AsyncSession,
    *,
    mode: ReadinessMode,
    now: datetime,
    event_key: str,
    staleness_hours: float,
) -> CheckResult:
    """Category 3 -- event_desk_state carries a recent freshness stamp.

    Absence is expected (and not a failure) in ``preflight`` mode, since no tick has
    run yet; it is a failure in ``post-tick`` mode, since the whole point of that
    mode is confirming the deliberate manual tick actually landed.
    """
    event_stmt = select(Event).where(
        Event.key == event_key,  # type: ignore[arg-type]
        Event.is_active.is_(True),  # type: ignore[attr-defined]
    )
    event = (await db.execute(event_stmt)).scalar_one_or_none()
    if event is None:
        if mode == "preflight":
            return CheckResult(
                category="freshness",
                status=ReadinessStatus.SKIP,
                message="No 'events' row yet (expected pre-tick); skipping freshness check.",
            )
        return CheckResult(
            category="freshness",
            status=ReadinessStatus.FAIL,
            message=(
                f"No active 'events' row found for key={event_key!r}; "
                "Job B has not ticked yet."
            ),
        )

    assert event.id is not None
    state_stmt = select(EventDeskState).where(
        EventDeskState.event_id == event.id  # type: ignore[arg-type]
    )
    state = (await db.execute(state_stmt)).scalar_one_or_none()
    if state is None:
        if mode == "preflight":
            return CheckResult(
                category="freshness",
                status=ReadinessStatus.SKIP,
                message="No event_desk_state row yet (expected pre-tick); skipping freshness check.",
            )
        return CheckResult(
            category="freshness",
            status=ReadinessStatus.FAIL,
            message=(
                f"No event_desk_state row found for event_id={event.id}; "
                "Job B has not ticked yet."
            ),
        )

    age = now - state.freshness_tick_at
    threshold = timedelta(hours=staleness_hours)
    if age > threshold:
        return CheckResult(
            category="freshness",
            status=ReadinessStatus.FAIL,
            message=(
                f"event_desk_state.freshness_tick_at="
                f"{state.freshness_tick_at.isoformat()} is stale ({age} old, exceeds "
                f"{staleness_hours}h threshold as of {now.isoformat()})."
            ),
        )
    return CheckResult(
        category="freshness",
        status=ReadinessStatus.PASS,
        message=(
            f"event_desk_state fresh as of {state.freshness_tick_at.isoformat()} "
            f"({age} old)."
        ),
    )


async def _check_render_snapshots(db: AsyncSession, *, event_key: str) -> CheckResult:
    """Category 4 -- render snapshots, only "when present" (materialization is separate).

    An event with zero materialized snapshots is not a readiness failure in either
    mode (snapshot materialization, launch-readiness item 10, is a separate ticket);
    a snapshot that *does* exist but carries a ``schema_version`` this build's codec
    no longer understands is.
    """
    event_stmt = select(Event).where(
        Event.key == event_key,  # type: ignore[arg-type]
        Event.is_active.is_(True),  # type: ignore[attr-defined]
    )
    event = (await db.execute(event_stmt)).scalar_one_or_none()
    if event is None:
        return CheckResult(
            category="render_snapshots",
            status=ReadinessStatus.SKIP,
            message="No 'events' row yet; nothing to check.",
        )

    assert event.id is not None
    snap_stmt = select(EventDeskRenderSnapshot).where(
        EventDeskRenderSnapshot.event_id == event.id  # type: ignore[arg-type]
    )
    snapshots = (await db.execute(snap_stmt)).scalars().all()
    if not snapshots:
        return CheckResult(
            category="render_snapshots",
            status=ReadinessStatus.SKIP,
            message="No render snapshots present yet; materialization not required for readiness.",
        )

    stale = [s for s in snapshots if s.schema_version != CURRENT_SCHEMA_VERSION]
    if stale:
        return CheckResult(
            category="render_snapshots",
            status=ReadinessStatus.FAIL,
            message=(
                f"{len(stale)} of {len(snapshots)} render snapshot(s) carry a stale "
                f"schema_version (expected {CURRENT_SCHEMA_VERSION}); rematerialize "
                "before enabling the cron."
            ),
        )
    return CheckResult(
        category="render_snapshots",
        status=ReadinessStatus.PASS,
        message=(
            f"{len(snapshots)} render snapshot(s) present, schema_version="
            f"{CURRENT_SCHEMA_VERSION} up to date."
        ),
    )


async def build_readiness_report(
    db: AsyncSession,
    *,
    mode: ReadinessMode,
    now: Optional[datetime] = None,
    event_key: str = EVENT_KEY_SUMMER_LEAGUE,
    staleness_hours: float = DEFAULT_STALENESS_HOURS,
) -> ReadinessReport:
    """Run every readiness category and return the combined report.

    Read-only: issues only ``SELECT`` statements over ``db`` and never calls
    ``db.add``/``db.execute`` with a write statement, ``db.commit``, or
    ``db.flush``.

    Args:
        db: Active database session (caller controls lifecycle; never committed here).
        mode: ``"preflight"`` (before enabling the cron) or ``"post-tick"`` (right
            after a deliberate manual tick).
        now: Override for "now" (tests only); defaults to the current UTC instant.
        event_key: The registered event's stable key. Defaults to Summer League's.
        staleness_hours: How old ``event_desk_state.freshness_tick_at`` may be
            before the freshness category fails in ``post-tick`` mode.

    Returns:
        A :class:`ReadinessReport` with one :class:`CheckResult` per category.
    """
    resolved_now = now if now is not None else datetime.utcnow()
    today_year = to_eastern_date(resolved_now).year

    results = (
        await _check_registration(
            db, mode=mode, today_year=today_year, event_key=event_key
        ),
        await _check_baselines(db),
        await _check_freshness(
            db,
            mode=mode,
            now=resolved_now,
            event_key=event_key,
            staleness_hours=staleness_hours,
        ),
        await _check_render_snapshots(db, event_key=event_key),
    )
    return ReadinessReport(mode=mode, now=resolved_now, results=results)


def _format_report(report: ReadinessReport) -> str:
    """Human-readable, CI-log-friendly rendering of a :class:`ReadinessReport`."""
    lines = [
        f"Summer League Desk readiness ({report.mode}) @ {report.now.isoformat()}:"
    ]
    for result in report.results:
        lines.append(
            f"  [{result.status.value.upper():4s}] {result.category}: {result.message}"
        )
    lines.append("READY" if report.ok else "NOT READY")
    return "\n".join(lines)


async def _run(args: argparse.Namespace) -> int:
    """Open a read-only session, build the report, print it, and return the exit code."""
    now = datetime.fromisoformat(args.now) if args.now else None
    async with SessionLocal() as db:
        report = await build_readiness_report(
            db, mode=args.mode, now=now, staleness_hours=args.staleness_hours
        )
    print(_format_report(report), flush=True)
    await engine.dispose()
    return 0 if report.ok else 1


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode",
        choices=["preflight", "post-tick"],
        help=(
            "'preflight' before enabling the Desk cron machine; 'post-tick' right "
            "after a deliberate manual tick to confirm it landed."
        ),
    )
    parser.add_argument(
        "--staleness-hours",
        type=float,
        default=DEFAULT_STALENESS_HOURS,
        help=(
            "How old event_desk_state.freshness_tick_at may be before the "
            "freshness category fails in post-tick mode (default: "
            f"{DEFAULT_STALENESS_HOURS})."
        ),
    )
    parser.add_argument(
        "--now",
        default=None,
        help=(
            "ISO-8601 datetime override for 'now' (tests/manual reruns only); "
            "defaults to the current UTC instant."
        ),
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Parse arguments, run the readiness checks, print the report, and return its exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
