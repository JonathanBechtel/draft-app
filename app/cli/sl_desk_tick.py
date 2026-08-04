"""Job B -- the Summer League Desk hourly tick (composite entrypoint).

**Since #699 this is one of four Desk entrypoints, and the conservative one.**
The tick's work is partitioned into three independently schedulable latency
classes (`docs/plans/summer-league-desk-simplification-spec.md` §2), each with
its own module and its own cron machine:

===================================  ==========  ===========  ==============
Entrypoint                           Cadence     Budget       Writer lock
===================================  ==========  ===========  ==============
``app.cli.sl_desk_fast_tick``        minutes     seconds      none
``app.cli.sl_desk_projection_tick``  ~hourly     < 1 min      none
``app.cli.sl_desk_backbone_tick``    hours       unbounded    the shared lock
``app.cli.sl_desk_tick`` (this)      hourly      2 min        the shared lock
===================================  ==========  ===========  ==============

This module runs all three in order inside one transaction, holding the shared
writer lock across the whole run -- byte-for-byte the pre-#699 behavior. It
stays because the production ``summer-league-desk-cron`` machine runs it until
the per-class machines are created and proven, so deploying the partition
needs no flag day and rolling back is a genuine rollback.

The order it wires (behavior spec §10 Job B), every step supplied by a sibling
ticket's already-shipped public API -- this module implements no grading,
storyline, or commentary logic of its own:

    0. schedule/scoreboard ingest (#515, #529)        -- fast class
    1. targeted live raw refresh (#531)               -- fast class
    2. normalize audited raw box scores               -- backbone class
    2b. scoped `summer_league_player_seasons` rebuild -- backbone class
    3. grades vs the active T1 baseline (#503/#548)   -- projection class
    4. storyline triggers for today's games (#504)    -- projection class
    5. commentary: all eight #520 detectors (#524)    -- projection class
    6. `event_desk_state` upsert (#506)               -- projection class
    7. render snapshot materialization (#551)         -- projection class

**Never rebuilds a distribution.** Job A (``scripts/build_sl_cohort_baselines.py``)
is the rare, offline cohort-baseline (T1) builder; this tick only ever reads
the currently active baseline version and fails loudly if none exists.

**Off-window / dormant tick is inert.** Before touching the network or any
T2/T3/T4 table the tick resolves the event's inner daily state; when that
resolves to ``None`` every step is skipped, ``event_desk_state`` and all render
snapshots stay byte-for-byte unchanged, and no state row is created merely to
claim freshness. The #527 pre-anchor bootstrap is the one exception: an event
whose window has arrived but has zero ``summer_league_games`` rows yet runs
scoreboard ingest once and re-resolves, since that step is exactly what creates
the anchor.

**Idempotent.** Every write delegates to an existing upsert, so re-running the
tick over the same data updates rows in place rather than duplicating them.

Run:
  scripts/with-db-env.sh conda run -n draftguru python -m app.cli.sl_desk_tick
  scripts/with-db-env.sh conda run -n draftguru python -m app.cli.sl_desk_tick \
      --raw-root data/raw/nba_stats/summer_league
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import AsyncSession

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
load_dotenv()

from app.cli._desk_class_runner import (  # noqa: E402
    CompletionFlags,
    parse_now,
    run_class_entrypoint,
)
from app.services.summer_league.desk_tick.composite import (  # noqa: E402
    DeskTickResult,
    run_desk_tick,
)
from app.services.summer_league.desk_tick.shared import (  # noqa: E402
    DEFAULT_RAW_ROOT,
    DeskLatencyClass,
)
from app.services.ingest.pipeline_telemetry import (  # noqa: E402
    PipelineTelemetry,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_RAW_ROOT",
    "DeskTickResult",
    "build_parser",
    "main",
    "run_desk_tick",
]


def _summarize(result: DeskTickResult) -> str:
    """Human-readable one-tick summary for the CLI entrypoint."""
    if result.dormant:
        suffix = (
            " (bootstrap ingest attempted, still no anchor)"
            if result.bootstrapped
            else ""
        )
        return (
            f"Summer League Desk tick executed_at={result.executed_at.isoformat()} "
            f"effective_data_at={result.now.isoformat()}: off-window (dormant) -- "
            f"no-op content_updated=false{suffix}."
        )

    lines = [
        f"Summer League Desk tick executed_at={result.executed_at.isoformat()} "
        f"effective_data_at={result.now.isoformat()}: "
        f"daily_state={result.daily_state.value if result.daily_state else None} "
        f"baseline_version={result.baseline_version}"
        f"{' (#527 bootstrap ingest ran)' if result.bootstrapped else ''}",
        f"  content_updated={str(result.content_updated).lower()} "
        f"source_refreshed={str(result.source_refreshed).lower()} "
        f"source_advanced={str(result.source_advanced).lower()}",
        f"  graded_players={len(result.graded_player_ids)} "
        f"normalized_competitions={list(result.normalized_competition_ids)}",
        f"  materialized_render_snapshot_variants={result.materialized_variant_count}",
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
    for competition_id, storyline_result in result.storyline_results.items():
        lines.append(
            f"  competition_id={competition_id}: slate_games={len(storyline_result.slate)} "
            f"quiet_hero={'yes' if storyline_result.quiet_slate_hero else 'no'}"
        )
    return "\n".join(lines)


def _completion_flags(result: DeskTickResult) -> CompletionFlags:
    """The composite earns every column, since it does every class's work."""
    return CompletionFlags(
        source_refreshed=result.source_refreshed,
        source_advanced=result.source_advanced,
        projections_refreshed=result.content_updated,
        metrics_rebuilt=bool(result.normalized_competition_ids),
        snapshots_materialized=bool(result.materialized_variant_count),
        content_updated=result.content_updated,
    )


async def _run(args: argparse.Namespace) -> None:
    """Run one production tick without holding a transaction across NBA I/O."""
    now = parse_now(args.now)

    async def _tick(db: AsyncSession, telemetry: PipelineTelemetry) -> DeskTickResult:
        return await run_desk_tick(
            db,
            now=now,
            raw_root=args.raw_root,
            release_transactions_for_network_io=True,
            telemetry=telemetry,
        )

    await run_class_entrypoint(
        DeskLatencyClass.COMPOSITE,
        run=_tick,
        completion_flags=_completion_flags,
        summarize=_summarize,
        entry_logger=logger,
        finish_fields=lambda result: {
            "executed_at": result.executed_at.isoformat(),
            "effective_data_at": result.now.isoformat(),
            "content_updated": result.content_updated,
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
            "ISO-8601 datetime override for 'now' (manual reruns/backfills "
            "only); defaults to the current UTC instant."
        ),
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Parse arguments and run one desk tick."""
    parser = build_parser()
    args = parser.parse_args(argv)
    asyncio.run(_run(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
