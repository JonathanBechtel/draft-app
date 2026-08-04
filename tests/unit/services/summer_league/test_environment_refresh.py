"""Unit tests for the Competition Context incremental-refresh orchestration (#618).

`app.services.summer_league.environment_refresh` wires the frozen #617
aggregation contract into the production pipeline. These tests avoid the
database entirely (per repo convention for `tests/unit/`): the module-level
`rebuild_environment_profiles`, `complete_pipeline`, and
`record_pipeline_failure` names are monkeypatched with fakes, and every call
passes a sentinel `db` object that the orchestration layer itself never
touches directly (it only ever forwards `db` to those faked dependencies).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest

from app.services import summer_league_environment_registry as registry_mod
from app.services.summer_league import environment_refresh as refresh_mod
from app.services.ingest.pipeline_telemetry import PipelineTelemetry
from app.services.summer_league_environment_service import EnvironmentRebuildResult


# --------------------------------------------------------------------------- #
# resolve_environment_refresh_scope
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "any_games,pending_reconciliation,expected",
    [
        (True, False, 2026),
        (False, True, 2026),
        (True, True, 2026),
        (False, False, None),
    ],
)
def test_resolve_environment_refresh_scope(
    any_games: bool, pending_reconciliation: bool, expected: int | None
) -> None:
    """Refresh is requested when new games appeared or work is pending; else skipped."""
    assert (
        refresh_mod.resolve_environment_refresh_scope(
            year=2026,
            any_games=any_games,
            pending_reconciliation=pending_reconciliation,
        )
        == expected
    )


# --------------------------------------------------------------------------- #
# is_environment_profile_stale
# --------------------------------------------------------------------------- #


def test_is_environment_profile_stale_within_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A profile computed well inside the threshold is not stale."""
    monkeypatch.setattr(
        registry_mod.settings, "summer_league_environment_stale_after_hours", 48
    )
    now = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
    calculated_at = now - timedelta(hours=10)
    assert refresh_mod.is_environment_profile_stale(calculated_at, now=now) is False


def test_is_environment_profile_stale_past_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A profile computed beyond the threshold is flagged stale."""
    monkeypatch.setattr(
        registry_mod.settings, "summer_league_environment_stale_after_hours", 48
    )
    now = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
    calculated_at = now - timedelta(hours=49)
    assert refresh_mod.is_environment_profile_stale(calculated_at, now=now) is True


def test_is_environment_profile_stale_exactly_at_threshold_is_not_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The boundary itself (exactly the threshold) is not yet stale (strict >)."""
    monkeypatch.setattr(
        registry_mod.settings, "summer_league_environment_stale_after_hours", 48
    )
    now = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
    calculated_at = now - timedelta(hours=48)
    assert refresh_mod.is_environment_profile_stale(calculated_at, now=now) is False


def test_is_environment_profile_stale_naive_timestamps_treated_as_utc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Naive datetimes (as stored on the schema) are compared as UTC, not local time."""
    monkeypatch.setattr(
        registry_mod.settings, "summer_league_environment_stale_after_hours", 48
    )
    now_naive = datetime(2026, 7, 19, 12, 0)  # no tzinfo, mirrors stored calculated_at
    calculated_naive = now_naive - timedelta(hours=49)
    assert (
        refresh_mod.is_environment_profile_stale(calculated_naive, now=now_naive)
        is True
    )


def test_is_environment_profile_stale_defaults_now_to_current_time() -> None:
    """Omitting `now` compares against the real current time."""
    ancient = datetime(2000, 1, 1, tzinfo=timezone.utc)
    assert refresh_mod.is_environment_profile_stale(ancient) is True


# --------------------------------------------------------------------------- #
# refresh_environment_profiles_for_year -- fakes for rebuild_environment_profiles
# and pipeline_state
# --------------------------------------------------------------------------- #


@dataclass
class _Recorder:
    """Captures calls into the faked pipeline-state helpers."""

    completed: list[dict[str, object]] = field(default_factory=list)
    failed: list[dict[str, object]] = field(default_factory=list)


@pytest.fixture()
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    rec = _Recorder()

    async def _fake_complete(db: object, **kwargs: object) -> None:
        rec.completed.append({"db": db, **kwargs})

    async def _fake_record_failure(db: object, **kwargs: object) -> None:
        rec.failed.append({"db": db, **kwargs})

    monkeypatch.setattr(refresh_mod, "complete_pipeline", _fake_complete)
    monkeypatch.setattr(refresh_mod, "record_pipeline_failure", _fake_record_failure)
    return rec


def _fake_rebuild(result: EnvironmentRebuildResult | Exception):
    async def _rebuild(db: object, *, year: int) -> EnvironmentRebuildResult:
        if isinstance(result, Exception):
            raise result
        return result

    return _rebuild


@pytest.mark.asyncio
async def test_refresh_success_records_completion(
    monkeypatch: pytest.MonkeyPatch, recorder: _Recorder
) -> None:
    """A clean rebuild (no per-scope failures) marks the job succeeded."""
    watermark = datetime(2026, 7, 19, 8, 0, tzinfo=timezone.utc)
    result = EnvironmentRebuildResult(
        requested_scopes=3,
        built_scopes=3,
        skipped_scopes=0,
        failed_scopes=0,
        metric_coverage_complete=12,
        input_watermark=watermark,
        published_scope_keys=["season:2026", "competition:1", "competition:2"],
    )
    monkeypatch.setattr(
        refresh_mod, "rebuild_environment_profiles", _fake_rebuild(result)
    )

    sentinel_db = object()
    outcome = await refresh_mod.refresh_environment_profiles_for_year(
        sentinel_db,
        year=2026,  # type: ignore[arg-type]
    )

    assert outcome.attempted is True
    assert outcome.succeeded is True
    assert outcome.built_scopes == 3
    assert outcome.failed_scopes == 0
    assert outcome.error is None
    assert outcome.input_watermark == watermark
    assert outcome.published_scope_keys == [
        "season:2026",
        "competition:1",
        "competition:2",
    ]

    assert len(recorder.completed) == 1
    assert recorder.completed[0]["db"] is sentinel_db
    assert recorder.completed[0]["job"] == refresh_mod._JOB
    assert recorder.completed[0]["metrics_rebuilt"] is False
    assert recorder.completed[0]["snapshots_materialized"] is False
    assert recorder.failed == []


@pytest.mark.asyncio
async def test_refresh_with_telemetry_wraps_step(
    monkeypatch: pytest.MonkeyPatch, recorder: _Recorder
) -> None:
    """A telemetry recorder, when given, wraps the rebuild in a named step."""
    result = EnvironmentRebuildResult(requested_scopes=1, built_scopes=1)
    monkeypatch.setattr(
        refresh_mod, "rebuild_environment_profiles", _fake_rebuild(result)
    )

    import logging

    telemetry = PipelineTelemetry(job="test", logger=logging.getLogger("test"))
    outcome = await refresh_mod.refresh_environment_profiles_for_year(
        object(),
        year=2026,
        telemetry=telemetry,  # type: ignore[arg-type]
    )
    assert outcome.succeeded is True


@pytest.mark.asyncio
async def test_refresh_partial_failure_records_failure_not_success(
    monkeypatch: pytest.MonkeyPatch, recorder: _Recorder
) -> None:
    """Some scopes failing validation marks the job failed and preserves the reasons.

    This does not mean the whole call raised -- per-scope isolation already
    happens inside `rebuild_environment_profiles` itself (#617); this
    orchestration layer just needs to surface that partial result honestly
    rather than reporting blanket success.
    """
    result = EnvironmentRebuildResult(
        requested_scopes=2,
        built_scopes=1,
        failed_scopes=1,
        failures={"competition:9": "forced validation failure"},
    )
    monkeypatch.setattr(
        refresh_mod, "rebuild_environment_profiles", _fake_rebuild(result)
    )

    outcome = await refresh_mod.refresh_environment_profiles_for_year(
        object(),
        year=2026,  # type: ignore[arg-type]
    )

    assert outcome.succeeded is False
    assert outcome.failures == {"competition:9": "forced validation failure"}
    assert recorder.completed == []
    assert len(recorder.failed) == 1
    assert "competition:9" in recorder.failed[0]["reason"]  # type: ignore[operator]


@pytest.mark.asyncio
async def test_refresh_isolates_unexpected_exception(
    monkeypatch: pytest.MonkeyPatch, recorder: _Recorder
) -> None:
    """An exception from the aggregation call is caught, never re-raised."""
    monkeypatch.setattr(
        refresh_mod,
        "rebuild_environment_profiles",
        _fake_rebuild(RuntimeError("db unavailable")),
    )

    outcome = await refresh_mod.refresh_environment_profiles_for_year(
        object(),
        year=2026,  # type: ignore[arg-type]
    )

    assert outcome.attempted is True
    assert outcome.succeeded is False
    assert outcome.error == "RuntimeError: db unavailable"
    assert recorder.completed == []
    assert len(recorder.failed) == 1
    assert recorder.failed[0]["reason"] == "RuntimeError: db unavailable"


@pytest.mark.asyncio
async def test_refresh_retried_call_is_idempotent_at_the_orchestration_layer(
    monkeypatch: pytest.MonkeyPatch, recorder: _Recorder
) -> None:
    """Calling refresh twice in a row (a retried cron cycle) succeeds both times.

    The underlying rebuild is independently proven idempotent (#617); this
    proves the orchestration wrapper itself introduces no retry hazard (e.g.
    no double-registration, no stateful call-count assumption).
    """
    result = EnvironmentRebuildResult(requested_scopes=1, built_scopes=1)
    monkeypatch.setattr(
        refresh_mod, "rebuild_environment_profiles", _fake_rebuild(result)
    )

    first = await refresh_mod.refresh_environment_profiles_for_year(
        object(),
        year=2026,  # type: ignore[arg-type]
    )
    second = await refresh_mod.refresh_environment_profiles_for_year(
        object(),
        year=2026,  # type: ignore[arg-type]
    )

    assert first.succeeded is True
    assert second.succeeded is True
    assert len(recorder.completed) == 2
