"""Summer League Desk -- **medium** latency class entrypoint (#699).

The projection builder. Runs ~hourly, is expected to finish in under a minute,
and **takes no writer lock**: it is a pure reader of canonical data plus a
writer of the Desk projection tables it exclusively owns (T2 grades, T3/T4
storylines and slates, ``event_desk_state``, render snapshots). See
`app.services.sources.summer_league.desk_tick.projection` and
`docs/plans/summer-league-desk-simplification-spec.md` §2.

Hourly is the intended promise and is not in question (spec "Product
decisions", settled: sub-hourly near-live updating is explicitly out of
scope). What #699 changes is that the hourly promise can now actually be kept
under peak load, because this class no longer queues behind an ~88-minute
venue ingest.

Run:
  scripts/with-db-env.sh conda run -n draftguru python -m app.cli.sl_desk_projection_tick
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import Sequence
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import AsyncSession

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
load_dotenv()

from app.services.sources.summer_league.desk_read import _effective_now  # noqa: E402
from app.cli._desk_class_runner import (  # noqa: E402
    CompletionFlags,
    parse_now,
    run_class_entrypoint,
)
from app.services.sources.summer_league.desk_tick.projection import (  # noqa: E402
    ProjectionTickResult,
    run_projection_tick,
)
from app.services.sources.summer_league.desk_tick.shared import (  # noqa: E402
    NO_WRITER_LOCK,
    DeskLatencyClass,
    TickContext,
)
from app.services.ingest.pipeline_telemetry import (  # noqa: E402
    PipelineTelemetry,
)

logger = logging.getLogger(__name__)


def _summarize(result: ProjectionTickResult) -> str:
    """Human-readable one-run summary for the cron log."""
    if result.dormant:
        return (
            f"Summer League Desk PROJECTION "
            f"executed_at={result.executed_at.isoformat()} "
            f"effective_data_at={result.now.isoformat()}: off-window (dormant) -- "
            f"no-op content_updated=false."
        )

    lines = [
        f"Summer League Desk PROJECTION "
        f"executed_at={result.executed_at.isoformat()} "
        f"effective_data_at={result.now.isoformat()}: "
        f"daily_state={result.daily_state.value if result.daily_state else None} "
        f"baseline_version={result.baseline_version}",
        f"  graded_players={len(result.graded_player_ids)} "
        f"materialized_render_snapshot_variants={result.materialized_variant_count}",
    ]
    for competition_id, storyline_result in result.storyline_results.items():
        lines.append(
            f"  competition_id={competition_id}: "
            f"slate_games={len(storyline_result.slate)} "
            f"quiet_hero={'yes' if storyline_result.quiet_slate_hero else 'no'}"
        )
    return "\n".join(lines)


def _completion_flags(result: ProjectionTickResult) -> CompletionFlags:
    """The projection class refreshes projections; it advances no source data.

    ``source_refreshed``/``source_advanced`` stay ``False`` deliberately --
    this class never talks to a provider. Stamping them here would make a
    rebuilt-from-stale-inputs projection look like fresh basketball data,
    which is exactly the inversion spec §1 forbids.
    """
    return CompletionFlags(
        projections_refreshed=result.content_updated,
        snapshots_materialized=bool(result.materialized_variant_count),
        content_updated=result.content_updated,
    )


async def _run(args: argparse.Namespace) -> None:
    """Run one projection-class rebuild."""
    override = parse_now(args.now)
    executed_at = override if override is not None else datetime.utcnow()
    resolved_now = _effective_now(executed_at, scheduled_write=True)

    async def _tick(
        db: AsyncSession, telemetry: PipelineTelemetry
    ) -> ProjectionTickResult:
        return await run_projection_tick(
            db,
            TickContext(
                now=resolved_now,
                executed_at=executed_at,
                telemetry=telemetry,
                lock=NO_WRITER_LOCK,
            ),
        )

    await run_class_entrypoint(
        DeskLatencyClass.PROJECTION,
        run=_tick,
        completion_flags=_completion_flags,
        summarize=_summarize,
        entry_logger=logger,
        finish_fields=lambda result: {
            "executed_at": result.executed_at.isoformat(),
            "effective_data_at": result.now.isoformat(),
            "dormant": result.dormant,
            "content_updated": result.content_updated,
        },
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(description=__doc__)
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
    """Parse arguments and run one projection rebuild."""
    parser = build_parser()
    args = parser.parse_args(argv)
    asyncio.run(_run(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
