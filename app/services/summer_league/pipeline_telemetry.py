"""Low-cardinality timing telemetry for Summer League scheduled pipelines."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from time import perf_counter
from uuid import uuid4


@dataclass
class PipelineTelemetry:
    """Emit one structured log record for each major pipeline step.

    Durations intentionally stay in application logs rather than becoming a
    second analytics store.  Durable coordination/freshness state belongs in
    :mod:`pipeline_state`; this class gives operators the run-level trace that
    explains why that state changed.
    """

    job: str
    logger: logging.Logger
    run_id: str = field(default_factory=lambda: uuid4().hex)
    _started_at: float = field(default_factory=perf_counter, init=False)

    @contextmanager
    def step(self, name: str):
        """Measure one named step and log its outcome even when it raises."""
        started_at = perf_counter()
        outcome = "succeeded"
        try:
            yield
        except Exception:
            outcome = "failed"
            raise
        finally:
            duration_ms = round((perf_counter() - started_at) * 1000, 1)
            self.logger.info(
                "summer_league_pipeline_step job=%s run_id=%s step=%s "
                "outcome=%s duration_ms=%s",
                self.job,
                self.run_id,
                name,
                outcome,
                duration_ms,
            )

    def finish(self, outcome: str) -> None:
        """Emit the run-level timing and final outcome."""
        duration_ms = round((perf_counter() - self._started_at) * 1000, 1)
        self.logger.info(
            "summer_league_pipeline_run job=%s run_id=%s outcome=%s duration_ms=%s",
            self.job,
            self.run_id,
            outcome,
            duration_ms,
        )
