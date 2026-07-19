"""Unit tests for the post-deploy Summer League cron image-digest verifier.

Covers the digest-comparison logic against mocked ``flyctl machine list``
JSON output: one machine on a stale digest, others matching the deployed app
image, and a machine missing entirely -- asserting each is reported/exits
correctly.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any

import pytest

from scripts import verify_cron_image_digests as verify_mod
from scripts.verify_cron_image_digests import (
    FlyctlError,
    build_machine_statuses,
    main,
    verify_cron_image_digests,
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


def _machines_fixture() -> list[dict[str, Any]]:
    """One matching cron, one stale-digest cron, plus the app machine."""
    return [
        _app_machine(),
        {
            "name": "summer-league-ingestion-cron",
            "config": {"image": CURRENT_IMAGE, "metadata": {}},
        },
        {
            "name": "summer-league-desk-cron",
            "config": {"image": STALE_IMAGE, "metadata": {}},
        },
    ]


@dataclass
class _FakeCompletedProcess:
    stdout: str
    returncode: int = 0
    stderr: str = ""


def _patch_flyctl(monkeypatch: pytest.MonkeyPatch, machines: list[dict[str, Any]]) -> None:
    def _fake_run(args: list[str], **kwargs: Any) -> _FakeCompletedProcess:
        assert args[:3] == ["flyctl", "machine", "list"]
        return _FakeCompletedProcess(stdout=json.dumps(machines))

    monkeypatch.setattr(verify_mod.subprocess, "run", _fake_run)


def test_build_machine_statuses_reports_match_and_drift() -> None:
    """A matching machine and a stale-digest machine are distinguished correctly."""
    machines = _machines_fixture()
    statuses = build_machine_statuses(
        machines, ["summer-league-ingestion-cron", "summer-league-desk-cron"]
    )
    by_name = {s.name: s for s in statuses}

    assert by_name["summer-league-ingestion-cron"].matches is True
    assert by_name["summer-league-ingestion-cron"].current_image == CURRENT_IMAGE

    assert by_name["summer-league-desk-cron"].matches is False
    assert by_name["summer-league-desk-cron"].current_image == STALE_IMAGE
    assert by_name["summer-league-desk-cron"].expected_image == CURRENT_IMAGE


def test_build_machine_statuses_reports_missing_machine() -> None:
    """A machine absent from the flyctl listing is reported as not found, not matched."""
    machines = _machines_fixture()
    statuses = build_machine_statuses(machines, ["summer-league-roster-cron"])
    assert len(statuses) == 1
    assert statuses[0].found is False
    assert statuses[0].matches is False
    assert statuses[0].current_image is None


def test_verify_cron_image_digests_returns_only_mismatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public function returns just the drifted/missing machine names."""
    _patch_flyctl(monkeypatch, _machines_fixture())

    result = verify_cron_image_digests(
        "draft-app-prod",
        [
            "summer-league-ingestion-cron",
            "summer-league-desk-cron",
            "summer-league-roster-cron",
        ],
    )

    assert result == ["summer-league-desk-cron", "summer-league-roster-cron"]


def test_verify_cron_image_digests_empty_when_all_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No mismatches means an empty list, per the documented contract."""
    machines = [
        _app_machine(),
        {
            "name": "summer-league-ingestion-cron",
            "config": {"image": CURRENT_IMAGE, "metadata": {}},
        },
    ]
    _patch_flyctl(monkeypatch, machines)

    result = verify_cron_image_digests("draft-app-prod", ["summer-league-ingestion-cron"])

    assert result == []


def test_run_flyctl_machine_list_raises_flyctl_error_on_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing flyctl invocation surfaces as FlyctlError, not a raw subprocess error."""

    def _fake_run(args: list[str], **kwargs: Any) -> _FakeCompletedProcess:
        raise subprocess.CalledProcessError(returncode=1, cmd=args, stderr="boom")

    monkeypatch.setattr(verify_mod.subprocess, "run", _fake_run)

    with pytest.raises(FlyctlError):
        verify_cron_image_digests("draft-app-prod", ["summer-league-desk-cron"])


def test_main_exits_nonzero_on_drift(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    """CLI entrypoint fails the check (not just warns) when a named machine drifts."""
    _patch_flyctl(monkeypatch, _machines_fixture())

    exit_code = main(
        [
            "--app",
            "draft-app-prod",
            "--machine",
            "summer-league-ingestion-cron",
            "--machine",
            "summer-league-desk-cron",
        ]
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "::error::" in captured.out
    assert "summer-league-desk-cron" in captured.out


def test_main_exits_zero_when_all_match(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI entrypoint passes when every named machine matches the app image."""
    machines = [
        _app_machine(),
        {
            "name": "summer-league-ingestion-cron",
            "config": {"image": CURRENT_IMAGE, "metadata": {}},
        },
    ]
    _patch_flyctl(monkeypatch, machines)

    exit_code = main(["--app", "draft-app-prod", "--machine", "summer-league-ingestion-cron"])

    assert exit_code == 0


def test_main_optional_machine_missing_does_not_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wholly-absent machine marked --optional-machine does not fail the check."""
    machines = [
        _app_machine(),
        {
            "name": "summer-league-ingestion-cron",
            "config": {"image": CURRENT_IMAGE, "metadata": {}},
        },
    ]
    _patch_flyctl(monkeypatch, machines)

    exit_code = main(
        [
            "--app",
            "draft-app-prod",
            "--machine",
            "summer-league-ingestion-cron",
            "--machine",
            "summer-league-desk-cron",
            "--optional-machine",
            "summer-league-desk-cron",
        ]
    )

    assert exit_code == 0


def test_main_optional_machine_still_fails_on_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    """--optional-machine only excuses absence, never an actual digest mismatch."""
    _patch_flyctl(monkeypatch, _machines_fixture())

    exit_code = main(
        [
            "--app",
            "draft-app-prod",
            "--machine",
            "summer-league-desk-cron",
            "--optional-machine",
            "summer-league-desk-cron",
        ]
    )

    assert exit_code == 1
