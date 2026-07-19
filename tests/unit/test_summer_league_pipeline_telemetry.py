"""Unit tests for Summer League cron timing telemetry."""

from __future__ import annotations

import logging

import pytest

from app.services.summer_league.pipeline_telemetry import PipelineTelemetry


def test_pipeline_telemetry_logs_step_and_run_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A successful step emits parseable job, run, step, outcome, and duration fields."""
    logger = logging.getLogger("tests.summer_league_pipeline_telemetry")
    telemetry = PipelineTelemetry(job="desk", logger=logger, run_id="run-123")

    with caplog.at_level(logging.INFO, logger=logger.name):
        with telemetry.step("normalization"):
            pass
        telemetry.finish("succeeded")

    messages = [record.getMessage() for record in caplog.records]
    assert (
        "summer_league_pipeline_step job=desk run_id=run-123 "
        "step=normalization outcome=succeeded duration_ms=" in messages[0]
    )
    assert (
        "summer_league_pipeline_run job=desk run_id=run-123 "
        "outcome=succeeded duration_ms=" in messages[1]
    )


def test_pipeline_telemetry_marks_a_raised_step_failed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A raised pipeline stage preserves its exception and logs a failed outcome."""
    logger = logging.getLogger("tests.summer_league_pipeline_telemetry")
    telemetry = PipelineTelemetry(job="full_ingestion", logger=logger, run_id="run-456")

    with caplog.at_level(logging.INFO, logger=logger.name), pytest.raises(RuntimeError):
        with telemetry.step("metrics_and_snapshots"):
            raise RuntimeError("rebuild failed")

    assert (
        "job=full_ingestion run_id=run-456 step=metrics_and_snapshots "
        "outcome=failed" in caplog.records[0].getMessage()
    )
