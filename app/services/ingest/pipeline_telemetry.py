"""Low-cardinality timing telemetry for Summer League scheduled pipelines."""

from __future__ import annotations

import logging
from collections.abc import Iterator
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
    def step(self, name: str, **fields: object) -> Iterator[dict[str, object]]:
        """Measure one named step and log its outcome even when it raises.

        Extra structured fields (e.g. ``writer_lock_wait_ms``,
        ``games_processed``) are folded into the SAME one-line structured log
        record this method already emits, alongside the existing
        ``job``/``run_id``/``step``/``outcome``/``duration_ms`` fields --
        never a second logging call -- so a value like a lock-wait duration
        or a batch's processed-row count stays a distinct greppable field
        rather than being folded into the generic ``duration_ms``.

        Args:
            name: The step name.
            fields: Extra fields known at call time; merged with (and
                overridable by) anything the caller sets on the yielded dict
                during the step.

        Yields:
            A mutable dict of extra fields, seeded with ``fields``. A caller
            that only needs the pre-existing five fields can ignore the
            yielded value entirely (``with telemetry.step(name): ...``);
            one that discovers a value mid-step (e.g. a lock-wait duration or
            a batch's row counts, only known after an inner call returns)
            can set it on the yielded dict and have it appear in this same
            step's log line.
        """
        started_at = perf_counter()
        outcome = "succeeded"
        extra_fields: dict[str, object] = dict(fields)
        try:
            yield extra_fields
        except Exception:
            outcome = "failed"
            raise
        finally:
            duration_ms = round((perf_counter() - started_at) * 1000, 1)
            extra = "".join(f" {key}={value}" for key, value in extra_fields.items())
            self.logger.info(
                "summer_league_pipeline_step job=%s run_id=%s step=%s "
                "outcome=%s duration_ms=%s%s",
                self.job,
                self.run_id,
                name,
                outcome,
                duration_ms,
                extra,
            )

    def finish(self, outcome: str, **fields: object) -> None:
        """Emit the run-level timing and final outcome."""
        duration_ms = round((perf_counter() - self._started_at) * 1000, 1)
        extra = "".join(f" {key}={value}" for key, value in fields.items())
        self.logger.info(
            "summer_league_pipeline_run job=%s run_id=%s outcome=%s duration_ms=%s%s",
            self.job,
            self.run_id,
            outcome,
            duration_ms,
            extra,
        )
