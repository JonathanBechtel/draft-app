"""Shared cron plumbing for the four Desk tick entrypoints (#699).

**Why this lives in ``app/cli/`` and not beside the classes it runs.** It is
the only piece of the partition that opens transactions and commits them, and
the repo forbids ``app/services/`` from calling ``commit()``/``rollback()``
(``scripts/check_request_transaction_policy.py``: services are request-bounded
code). That rule is worth keeping at full strength, so transaction control
stays on the CLI side of the line -- the latency classes under
``app/services/summer_league/desk_tick/`` declare *where* a transaction may be
released via ``TickContext.transaction_boundary``, and this module decides
*how*.

Every latency class runs the same operational shape -- open a session, stamp a
``summer_league_pipeline_states`` start token, run, stamp completion or
failure, emit run-level telemetry, print a summary -- and only the *class* and
the *completion flags* differ. Keeping that here means the four
`app/cli/sl_desk_*.py` modules are genuinely thin entrypoints rather than four
copies of the same try/except/rollback dance drifting apart over time.

**Per-class pipeline rows are the point** (#699 "per-class telemetry so 'the
tick was slow' resolves to *which* class"). Each class writes its own
``summer_league_pipeline_states`` row keyed by its own
:class:`~app.schemas.summer_league_pipeline.SummerLeaguePipelineJob`, so a
backbone run that takes 40 minutes and a fast run that takes 3 seconds are
never averaged into one number, and a failed backbone leaves the fast class's
freshness row untouched -- which is exactly the "classes fail independently"
acceptance criterion.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.summer_league.desk_tick.shared import DeskLatencyClass
from app.services.summer_league.pipeline_state import (
    complete_pipeline,
    record_pipeline_failure,
    start_pipeline,
)
from app.services.summer_league.pipeline_telemetry import PipelineTelemetry
from app.utils.db_async import SessionLocal, engine

logger = logging.getLogger(__name__)

ResultT = TypeVar("ResultT")


@dataclass(frozen=True)
class CompletionFlags:
    """Which freshness columns a class's successful run is entitled to stamp.

    A class must only claim what it actually did. The fast class refreshes
    source data but builds no projection; the projection class refreshes
    projections but advances no source; the backbone rebuilds metrics. Letting
    each stamp only its own columns is what keeps
    ``summer_league_pipeline_states`` an honest operational record rather than
    three jobs overwriting one blurred timestamp -- the failure spec §1 names
    as "the wrong one is shown to users".
    """

    source_refreshed: bool = False
    source_advanced: bool = False
    projections_refreshed: bool = False
    metrics_rebuilt: bool = False
    snapshots_materialized: bool = False
    content_updated: bool = False


async def run_class_entrypoint(
    latency_class: DeskLatencyClass,
    *,
    run: Callable[[AsyncSession, PipelineTelemetry], Awaitable[ResultT]],
    completion_flags: Callable[[ResultT], CompletionFlags],
    summarize: Callable[[ResultT], str],
    entry_logger: Optional[logging.Logger] = None,
    finish_fields: Callable[[ResultT], dict[str, object]] | None = None,
) -> ResultT:
    """Run one latency class as a scheduled job, with durable state and telemetry.

    Args:
        latency_class: The class being run; supplies both the telemetry
            ``job`` label and the ``summer_league_pipeline_states`` row.
        run: The class runner, given a session and this run's telemetry. The
            session's transaction is committed on success and rolled back on
            failure by this function.
        completion_flags: Maps the runner's result to the freshness columns
            this run earned.
        summarize: Human-readable one-run summary for the cron log.
        entry_logger: Logger for telemetry records; defaults to this module's.
        finish_fields: Extra structured fields for the run-level telemetry
            record.

    Returns:
        Whatever ``run`` returned.

    Raises:
        Exception: Anything ``run`` raises, after the failure has been
            recorded durably and the transaction rolled back.
    """
    telemetry = PipelineTelemetry(
        job=latency_class.value, logger=entry_logger or logger
    )
    job = latency_class.pipeline_job
    async with SessionLocal() as db:
        pipeline_state = await start_pipeline(
            db,
            job=job,
            job_image=os.getenv("FLY_IMAGE_REF") or os.getenv("FLY_IMAGE"),
        )
        pipeline_started_at = pipeline_state.last_started_at
        assert pipeline_started_at is not None
        await db.commit()
        try:
            with telemetry.step(latency_class.value):
                result = await run(db, telemetry)
            flags = completion_flags(result)
            await complete_pipeline(
                db,
                job=job,
                metrics_rebuilt=flags.metrics_rebuilt,
                snapshots_materialized=flags.snapshots_materialized,
                source_refreshed=flags.source_refreshed,
                source_advanced=flags.source_advanced,
                projections_refreshed=flags.projections_refreshed,
                content_updated=flags.content_updated,
                started_at=pipeline_started_at,
            )
            await db.commit()
        except Exception as exc:
            await db.rollback()
            try:
                async with db.begin():
                    await record_pipeline_failure(
                        db,
                        job=job,
                        reason=f"{type(exc).__name__}: {exc}",
                        started_at=pipeline_started_at,
                    )
            except Exception:
                logger.exception(
                    "Could not record failed Summer League %s run", latency_class.value
                )
            telemetry.finish("failed", content_updated=False)
            raise
    telemetry.finish(
        "succeeded", **(finish_fields(result) if finish_fields is not None else {})
    )
    print(summarize(result), flush=True)
    await engine.dispose()
    return result


def parse_now(raw: Optional[str]) -> Optional[datetime]:
    """Parse the shared ``--now`` override, or ``None`` for the real clock."""
    return datetime.fromisoformat(raw) if raw else None
