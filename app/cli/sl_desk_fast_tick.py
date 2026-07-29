"""Summer League Desk -- **fast** latency class entrypoint (#699).

The live poller. Runs every few minutes, is expected to finish in seconds, and
**takes no writer lock at all**, so it cannot be starved by the backbone no
matter how long a venue ingest runs. See
`app.services.summer_league.desk_tick.fast` for what it does and why, and
`docs/plans/summer-league-desk-simplification-spec.md` §2 for the partition.

This is the entrypoint whose reliability *is* the ticket's acceptance signal:
the percentage of its scheduled runs that complete with advanced source data,
measured **specifically within live-game windows** -- never daily-averaged,
because off-peak runs succeed easily and mask the live-window misses that are
the entire user-visible problem.

Run:
  scripts/with-db-env.sh conda run -n draftguru python -m app.cli.sl_desk_fast_tick
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
from app.cli._desk_class_runner import (  # noqa: E402
    CompletionFlags,
    parse_now,
    run_class_entrypoint,
)
from app.services.summer_league.desk_tick.fast import (  # noqa: E402
    FastTickResult,
    run_fast_tick,
)
from app.services.summer_league.desk_tick.shared import (  # noqa: E402
    DEFAULT_RAW_ROOT,
    NO_WRITER_LOCK,
    DeskLatencyClass,
    TickContext,
)
from app.services.summer_league.pipeline_telemetry import (  # noqa: E402
    PipelineTelemetry,
)

logger = logging.getLogger(__name__)


def _summarize(result: FastTickResult) -> str:
    """Human-readable one-run summary for the cron log."""
    if result.dormant:
        suffix = (
            " (bootstrap ingest attempted, still no anchor)"
            if result.bootstrapped
            else ""
        )
        return (
            f"Summer League Desk FAST executed_at={result.executed_at.isoformat()} "
            f"effective_data_at={result.now.isoformat()}: off-window (dormant) -- "
            f"no-op{suffix}."
        )

    lines = [
        f"Summer League Desk FAST executed_at={result.executed_at.isoformat()} "
        f"effective_data_at={result.now.isoformat()}: "
        f"daily_state={result.daily_state.value if result.daily_state else None}"
        f"{' (#527 bootstrap ingest ran)' if result.bootstrapped else ''}",
        f"  source_refreshed={str(result.source_refreshed).lower()} "
        f"source_advanced={str(result.source_advanced).lower()}",
    ]
    if result.scoreboard_report is not None:
        report = result.scoreboard_report
        lines.append(
            f"  scoreboard: checked={report.competitions_checked} "
            f"created={report.games_created} updated={report.games_updated} "
            f"errors={report.errors} unresolved_team_ids={report.unresolved_team_ids}"
        )
    if result.live_refresh_report is not None:
        refresh = result.live_refresh_report
        lines.append(
            f"  live_refresh: selected={refresh.selected} groups={refresh.groups} "
            f"written={refresh.written} errors={refresh.errors}"
        )
    return "\n".join(lines)


def _completion_flags(result: FastTickResult) -> CompletionFlags:
    """The fast class refreshes source data and builds no projection.

    ``projections_refreshed``/``snapshots_materialized``/``metrics_rebuilt``
    stay ``False`` deliberately: claiming them here would let a healthy poller
    mask a projection class that has not run in hours.
    """
    return CompletionFlags(
        source_refreshed=result.source_refreshed,
        source_advanced=result.source_advanced,
    )


async def _run(args: argparse.Namespace) -> None:
    """Run one fast-class poll."""
    override = parse_now(args.now)
    executed_at = override if override is not None else datetime.utcnow()
    resolved_now = _effective_now(executed_at, scheduled_write=True)

    async def _tick(db: AsyncSession, telemetry: PipelineTelemetry) -> FastTickResult:
        return await run_fast_tick(
            db,
            TickContext(
                now=resolved_now,
                executed_at=executed_at,
                raw_root=args.raw_root,
                # Only `app/cli/` decides *how* a transaction ends; the class
                # only declares where it may (see TickContext).
                transaction_boundary=db.commit,
                telemetry=telemetry,
                lock=NO_WRITER_LOCK,
            ),
        )

    await run_class_entrypoint(
        DeskLatencyClass.FAST,
        run=_tick,
        completion_flags=_completion_flags,
        summarize=_summarize,
        entry_logger=logger,
        finish_fields=lambda result: {
            "executed_at": result.executed_at.isoformat(),
            "effective_data_at": result.now.isoformat(),
            "dormant": result.dormant,
            "source_refreshed": result.source_refreshed,
            "source_advanced": result.source_advanced,
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
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Parse arguments and run one fast-class poll."""
    parser = build_parser()
    args = parser.parse_args(argv)
    asyncio.run(_run(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
