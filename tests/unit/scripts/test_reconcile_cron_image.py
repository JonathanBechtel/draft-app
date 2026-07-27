"""Unit tests for the Desk cron stop-wait-timeout reconciliation retry.

Exercises :func:`scripts.reconcile_cron_image.reconcile_cron_machine_image`'s
branches (already current, missing, still running after the retry wait,
and the full update+restart happy path) against a fake ``flyctl`` process
runner -- no real subprocess or network calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

import pytest

from scripts import reconcile_cron_image as reconcile_mod
from scripts.reconcile_cron_image import (
    ReconcileOutcome,
    main,
    reconcile_cron_machine_image,
)

CURRENT_IMAGE = "registry.fly.io/draft-app-prod:deployment-01CURRENT"
STALE_IMAGE = "registry.fly.io/draft-app-prod:deployment-00STALE"


def _app_machine() -> dict[str, Any]:
    return {
        "name": "draft-app-prod-app-abc123",
        "config": {
            "image": CURRENT_IMAGE,
            "metadata": {"fly_process_group": "app"},
        },
    }


def _desk_machine(image: str) -> dict[str, Any]:
    return {
        "id": "148e2e9a123456",
        "name": "summer-league-desk-cron",
        "config": {"image": image, "metadata": {}},
    }


@dataclass
class _FakeCompletedProcess:
    returncode: int = 0
    stderr: str = ""


@dataclass
class _RecordingRunner:
    """Fake ``flyctl`` invoker: records calls, returns canned exit codes per subcommand."""

    wait_returncode: int = 0
    update_returncode: int = 0
    start_returncode: int = 0
    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, args: Sequence[str]) -> _FakeCompletedProcess:
        self.calls.append(list(args))
        subcommand = args[2] if len(args) > 2 else ""
        if subcommand == "wait":
            return _FakeCompletedProcess(
                returncode=self.wait_returncode, stderr="timed out"
            )
        if subcommand == "update":
            return _FakeCompletedProcess(
                returncode=self.update_returncode, stderr="update failed"
            )
        if subcommand == "start":
            return _FakeCompletedProcess(
                returncode=self.start_returncode, stderr="start failed"
            )
        raise AssertionError(f"unexpected flyctl subcommand invocation: {args}")


def _patch_machine_list(
    monkeypatch: pytest.MonkeyPatch, machines: list[dict[str, Any]]
) -> None:
    monkeypatch.setattr(reconcile_mod, "_run_flyctl_machine_list", lambda app: machines)
    monkeypatch.setattr(
        reconcile_mod,
        "_resolve_app_image",
        lambda machines: next(
            (
                m["config"]["image"]
                for m in machines
                if m.get("config", {}).get("metadata", {}).get("fly_process_group")
                == "app"
            ),
            None,
        ),
    )


def test_already_current_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """A machine already on the current image needs no flyctl mutation calls."""
    _patch_machine_list(monkeypatch, [_app_machine(), _desk_machine(CURRENT_IMAGE)])
    runner = _RecordingRunner()

    result = reconcile_cron_machine_image(
        "draft-app-prod", "summer-league-desk-cron", run=runner
    )

    assert result.outcome == ReconcileOutcome.ALREADY_CURRENT
    assert result.ok is True
    assert runner.calls == []


def test_machine_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """A machine absent from the listing reports MACHINE_NOT_FOUND, not an error."""
    _patch_machine_list(monkeypatch, [_app_machine()])
    runner = _RecordingRunner()

    result = reconcile_cron_machine_image(
        "draft-app-prod", "summer-league-desk-cron", run=runner
    )

    assert result.outcome == ReconcileOutcome.MACHINE_NOT_FOUND
    assert result.ok is True
    assert runner.calls == []


def test_still_running_after_retry_wait_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A machine that still hasn't stopped within the retry window is left alone."""
    _patch_machine_list(monkeypatch, [_app_machine(), _desk_machine(STALE_IMAGE)])
    runner = _RecordingRunner(wait_returncode=1)

    result = reconcile_cron_machine_image(
        "draft-app-prod",
        "summer-league-desk-cron",
        wait_timeout_s=120,
        run=runner,
    )

    assert result.outcome == ReconcileOutcome.STILL_RUNNING
    assert result.ok is False
    # Only the wait was attempted -- update/start must not run on a live machine.
    assert len(runner.calls) == 1
    assert runner.calls[0][2] == "wait"


def test_full_update_and_restart_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Once stopped, the machine is updated with --skip-start then restarted."""
    _patch_machine_list(monkeypatch, [_app_machine(), _desk_machine(STALE_IMAGE)])
    runner = _RecordingRunner()

    result = reconcile_cron_machine_image(
        "draft-app-prod", "summer-league-desk-cron", run=runner
    )

    assert result.outcome == ReconcileOutcome.UPDATED
    assert result.ok is True
    subcommands = [call[2] for call in runner.calls]
    assert subcommands == ["wait", "update", "start"]
    update_call = runner.calls[1]
    assert "--skip-start" in update_call
    assert CURRENT_IMAGE in update_call


def test_command_is_re_declared_with_the_image(monkeypatch: pytest.MonkeyPatch) -> None:
    """The retry path must set argv too, not just the image.

    A machine's argv is frozen at `flyctl machine run` time, so an image-only
    update here lands a new image on a machine still pointing at an entrypoint
    the new image may no longer contain. The digest verifier then passes -- the
    *image* is current -- while every tick dies. This is the shape that broke
    the stage Desk cron after #685.
    """
    _patch_machine_list(monkeypatch, [_app_machine(), _desk_machine(STALE_IMAGE)])
    runner = _RecordingRunner()

    result = reconcile_cron_machine_image(
        "draft-app-prod",
        "summer-league-desk-cron",
        command="-m app.cli.sl_desk_tick",
        run=runner,
    )

    assert result.outcome == ReconcileOutcome.UPDATED
    update_call = runner.calls[1]
    assert "--command" in update_call
    assert update_call[update_call.index("--command") + 1] == "-m app.cli.sl_desk_tick"
    assert CURRENT_IMAGE in update_call


def test_command_is_omitted_when_not_supplied(monkeypatch: pytest.MonkeyPatch) -> None:
    """Callers that know argv is already correct keep the previous behaviour.

    Passing an empty `--command` through to flyctl would blank the machine's
    argv, so absence must mean "leave it alone", not "set it to nothing".
    """
    _patch_machine_list(monkeypatch, [_app_machine(), _desk_machine(STALE_IMAGE)])
    runner = _RecordingRunner()

    reconcile_cron_machine_image(
        "draft-app-prod", "summer-league-desk-cron", run=runner
    )

    assert "--command" not in runner.calls[1]


def test_cli_forwards_the_command_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--command` reaches flyctl from the CLI the prod workflow actually invokes."""
    _patch_machine_list(monkeypatch, [_app_machine(), _desk_machine(STALE_IMAGE)])
    runner = _RecordingRunner()
    monkeypatch.setattr(reconcile_mod, "_default_run", runner)

    exit_code = main(
        [
            "--app",
            "draft-app-prod",
            "--machine",
            "summer-league-desk-cron",
            "--command",
            "-m app.cli.sl_desk_tick",
        ]
    )

    assert exit_code == 0
    update_call = next(c for c in runner.calls if c[2] == "update")
    assert update_call[update_call.index("--command") + 1] == "-m app.cli.sl_desk_tick"


def test_update_failure_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed `machine update` call surfaces as UPDATE_FAILED, not silently ignored."""
    _patch_machine_list(monkeypatch, [_app_machine(), _desk_machine(STALE_IMAGE)])
    runner = _RecordingRunner(update_returncode=1)

    result = reconcile_cron_machine_image(
        "draft-app-prod", "summer-league-desk-cron", run=runner
    )

    assert result.outcome == ReconcileOutcome.UPDATE_FAILED
    assert result.ok is False


def test_start_failure_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed re-arm `machine start` call surfaces as START_FAILED."""
    _patch_machine_list(monkeypatch, [_app_machine(), _desk_machine(STALE_IMAGE)])
    runner = _RecordingRunner(start_returncode=1)

    result = reconcile_cron_machine_image(
        "draft-app-prod", "summer-league-desk-cron", run=runner
    )

    assert result.outcome == ReconcileOutcome.START_FAILED
    assert result.ok is False


def test_main_exits_nonzero_when_still_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI entrypoint fails loudly (not a bare warning) when reconciliation can't land."""
    _patch_machine_list(monkeypatch, [_app_machine(), _desk_machine(STALE_IMAGE)])
    monkeypatch.setattr(
        reconcile_mod,
        "_default_run",
        _RecordingRunner(wait_returncode=1),
    )

    exit_code = main(
        ["--app", "draft-app-prod", "--machine", "summer-league-desk-cron"]
    )

    assert exit_code == 1


def test_main_exits_zero_when_already_current(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI entrypoint passes cleanly when there is nothing to reconcile."""
    _patch_machine_list(monkeypatch, [_app_machine(), _desk_machine(CURRENT_IMAGE)])
    monkeypatch.setattr(reconcile_mod, "_default_run", _RecordingRunner())

    exit_code = main(
        ["--app", "draft-app-prod", "--machine", "summer-league-desk-cron"]
    )

    assert exit_code == 0
