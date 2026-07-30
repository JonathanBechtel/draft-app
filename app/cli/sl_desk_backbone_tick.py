"""Summer League Desk -- **slow** latency class entrypoint (#699).

The backbone: normalize audited raw box scores, then rebuild the metrics that
normalization invalidated. Runs on an hours/off-peak cadence, has **no latency
budget**, and is the one class that still takes the shared Summer League writer
lock -- it writes broad canonical state and must stay serialized against the
full ingestion runner. See `app.services.summer_league.desk_tick.backbone` and
`docs/plans/summer-league-desk-simplification-spec.md` §2.

**Its cost is now structurally invisible to the other two classes.** The fast
poller and the projection builder take no lock, so however long this runs it
cannot starve them -- which is the entire point of the partition. It does not
make this class *cheaper*: #701 (the rebuild still doing a full-pool
``compute()`` on every in-event run) is open and separate, and this entrypoint
inherits that cost.

Run:
  scripts/with-db-env.sh conda run -n draftguru python -m app.cli.sl_desk_backbone_tick
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import AsyncSession

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
load_dotenv()

from app.services.summer_league.desk_read import _effective_now  # noqa: E402
from app.services.summer_league.desk_tick.backbone import (  # noqa: E402
    BackboneTickResult,
    run_backbone_tick,
)
from app.cli._desk_class_runner import (  # noqa: E402
    CompletionFlags,
    parse_now,
    run_class_entrypoint,
)
from app.services.summer_league.desk_tick.shared import (  # noqa: E402
    DEFAULT_RAW_ROOT,
    DEFAULT_WRITER_LOCK_MAX_WAIT_SECONDS,
    DeskLatencyClass,
    TickContext,
    WriterLockPolicy,
)
from app.services.summer_league.pipeline_telemetry import (  # noqa: E402
    PipelineTelemetry,
)

logger = logging.getLogger(__name__)

# The backbone has no latency budget of its own, so it can afford to wait far
# longer than the Desk's old 30s bound before conceding the lock to the full
# ingestion runner. The short bound existed to stop a *user-facing* surface
# being starved; that surface is no longer behind this lock.
DEFAULT_BACKBONE_LOCK_MAX_WAIT_SECONDS = 300.0


def _summarize(result: BackboneTickResult) -> str:
    """Human-readable one-run summary for the cron log."""
    if result.dormant:
        return (
            f"Summer League Desk BACKBONE "
            f"executed_at={result.executed_at.isoformat()} "
            f"effective_data_at={result.now.isoformat()}: off-window (dormant) -- "
            f"no-op."
        )
    return (
        f"Summer League Desk BACKBONE "
        f"executed_at={result.executed_at.isoformat()} "
        f"effective_data_at={result.now.isoformat()}: "
        f"daily_state={result.daily_state.value if result.daily_state else None}\n"
        f"  normalized_competitions={list(result.normalized_competition_ids)} "
        f"metrics_rebuilt={str(result.metrics_rebuilt).lower()} "
        f"source_advanced={str(result.source_advanced).lower()}"
    )


def _completion_flags(result: BackboneTickResult) -> CompletionFlags:
    """The backbone advances canonical source data and rebuilds metrics.

    ``projections_refreshed``/``snapshots_materialized``/``content_updated``
    stay ``False``: this class writes no Desk projection, and claiming it did
    would let a healthy backbone mask a projection class that has stopped.
    """
    return CompletionFlags(
        source_advanced=result.source_advanced,
        metrics_rebuilt=result.metrics_rebuilt,
    )


async def _run(args: argparse.Namespace) -> None:
    """Run one backbone normalize + scoped metrics rebuild."""
    override = parse_now(args.now)
    executed_at = override if override is not None else datetime.utcnow()
    resolved_now = _effective_now(executed_at, scheduled_write=True)

    async def _tick(
        db: AsyncSession, telemetry: PipelineTelemetry
    ) -> BackboneTickResult:
        return await run_backbone_tick(
            db,
            TickContext(
                now=resolved_now,
                executed_at=executed_at,
                raw_root=args.raw_root,
                transaction_boundary=db.commit,
                telemetry=telemetry,
                lock=WriterLockPolicy(
                    enabled=True,
                    max_wait_seconds=args.writer_lock_max_wait_seconds,
                    telemetry=telemetry,
                ),
            ),
        )

    await run_class_entrypoint(
        DeskLatencyClass.BACKBONE,
        run=_tick,
        completion_flags=_completion_flags,
        summarize=_summarize,
        entry_logger=logger,
        finish_fields=lambda result: {
            "executed_at": result.executed_at.isoformat(),
            "effective_data_at": result.now.isoformat(),
            "dormant": result.dormant,
            "source_advanced": result.source_advanced,
            "metrics_rebuilt": result.metrics_rebuilt,
        },
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--raw-root",
        type=Path,
        default=DEFAULT_RAW_ROOT,
        help="Root directory of audited raw Summer League snapshots.",
    )
    parser.add_argument(
        "--now",
        default=None,
        help=(
            "ISO-8601 datetime override for 'now' (manual reruns only); "
            "defaults to the current UTC instant."
        ),
    )
    parser.add_argument(
        "--writer-lock-max-wait-seconds",
        type=float,
        default=DEFAULT_BACKBONE_LOCK_MAX_WAIT_SECONDS,
        help=(
            "Maximum wall-clock wait for the shared Summer League writer lock "
            f"(default {DEFAULT_BACKBONE_LOCK_MAX_WAIT_SECONDS:.0f}s; the Desk's "
            f"old user-facing bound was {DEFAULT_WRITER_LOCK_MAX_WAIT_SECONDS:.0f}s)."
        ),
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Parse arguments and run one backbone pass."""
    parser = build_parser()
    args = parser.parse_args(argv)
    asyncio.run(_run(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
