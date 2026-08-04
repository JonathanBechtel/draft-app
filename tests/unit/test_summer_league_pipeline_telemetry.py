"""Unit tests for Summer League cron timing telemetry."""

from __future__ import annotations

import logging

import pytest

from app.services.ingest.pipeline_telemetry import PipelineTelemetry


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


def test_pipeline_telemetry_step_logs_call_time_extra_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Extra fields passed at call time appear in the same structured log line."""
    logger = logging.getLogger("tests.summer_league_pipeline_telemetry")
    telemetry = PipelineTelemetry(job="desk", logger=logger, run_id="run-789")

    with caplog.at_level(logging.INFO, logger=logger.name):
        with telemetry.step("writer_lock_wait", writer_lock_wait_ms=12.3):
            pass

    message = caplog.records[0].getMessage()
    assert (
        "summer_league_pipeline_step job=desk run_id=run-789 "
        "step=writer_lock_wait outcome=succeeded duration_ms=" in message
    )
    assert "writer_lock_wait_ms=12.3" in message


def test_pipeline_telemetry_step_logs_fields_set_during_the_step(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Fields set on the yielded dict mid-step land in the same log line.

    e.g. fields discovered only after an inner call returns are folded into
    the same log line as fields known at call time.
    """
    logger = logging.getLogger("tests.summer_league_pipeline_telemetry")
    telemetry = PipelineTelemetry(job="full_ingestion", logger=logger, run_id="run-abc")

    with caplog.at_level(logging.INFO, logger=logger.name):
        with telemetry.step("venue:15:shot_batch_1") as fields:
            fields["games_processed"] = 8
            fields["events_processed"] = 214

    message = caplog.records[0].getMessage()
    assert "step=venue:15:shot_batch_1" in message
    assert "games_processed=8" in message
    assert "events_processed=214" in message


def test_pipeline_telemetry_step_with_no_fields_matches_prior_output_shape(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A bare `step(name)` call (no fields) logs identically to before this change.

    Backward-compatibility guard: no trailing extra-field noise appears after
    the numeric `duration_ms` value when no fields were ever set.
    """
    logger = logging.getLogger("tests.summer_league_pipeline_telemetry")
    telemetry = PipelineTelemetry(job="desk", logger=logger, run_id="run-def")

    with caplog.at_level(logging.INFO, logger=logger.name):
        with telemetry.step("normalization"):
            pass

    message = caplog.records[0].getMessage()
    duration_value = message.rsplit("duration_ms=", 1)[1]
    assert " " not in duration_value
    float(duration_value)  # the trailing token is purely numeric, nothing appended
