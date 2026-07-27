"""Unit tests for the deploy-freshness checker.

Failure this descends from
--------------------------
Incident #669: production ran a 3.5-day-old image through the entire Summer League
window while the fixes for the fault actively unfolding sat merged on ``main``. Nothing
reported the gap. These tests pin the behaviors that make this checker a signal rather
than decoration:

* it measures distance from a *branch*, not from the app image (the axis nothing watched);
* it treats app machines disagreeing with each other as always-wrong, not as lag;
* it degrades to a reported "unknown" instead of raising, because a monitoring check that
  dies produces the same silence it exists to break;
* an unreachable Fly API is not the same as a stale deploy.

``flyctl`` and ``git`` are both stubbed; nothing here touches a network or a real repo.
"""

from __future__ import annotations

from typing import Any

import pytest

from scripts import check_deploy_freshness as freshness_mod
from scripts.check_deploy_freshness import (
    GitError,
    build_report,
    is_stale,
    main,
)


def _app_machine(sha: str | None, *, process_group: str = "app") -> dict[str, Any]:
    """A machine payload shaped like real ``flyctl machine list --json`` output."""
    labels = {"GH_REPO": "JonathanBechtel/draft-app"}
    if sha is not None:
        labels["GH_SHA"] = sha
    return {
        "name": f"machine-{sha or 'nolabel'}",
        "config": {"metadata": {"fly_process_group": process_group}},
        "image_ref": {"labels": labels},
    }


@pytest.fixture
def fake_git(monkeypatch: pytest.MonkeyPatch):
    """Stub ``_git`` with a configurable command -> output mapping."""

    def install(responses: dict[tuple[str, ...], str]):
        def _fake(*args: str) -> str:
            if args in responses:
                value = responses[args]
                if isinstance(value, Exception):
                    raise value
                return value
            raise GitError(f"unexpected git call: {args}")

        monkeypatch.setattr(freshness_mod, "_git", _fake)

    return install


def test_reports_current_when_deployed_matches_target(fake_git) -> None:
    """The healthy case: deployed commit is the branch tip."""
    fake_git({("rev-parse", "origin/main"): "abc123"})

    report = build_report("app", [_app_machine("abc123")], target_ref="origin/main")

    assert report.is_current is True
    assert report.commits_behind is None or report.commits_behind == 0


def test_measures_distance_from_the_branch(fake_git) -> None:
    """The #669 shape: production behind main, with the gap quantified."""
    fake_git(
        {
            ("rev-parse", "origin/main"): "newsha",
            ("cat-file", "-e", "oldsha^{commit}"): "",
            ("rev-list", "--count", "oldsha..newsha"): "37",
            ("show", "-s", "--format=%cI", "oldsha"): "2026-07-19T22:39:00+00:00",
        }
    )

    report = build_report("app", [_app_machine("oldsha")], target_ref="origin/main")

    assert report.is_current is False
    assert report.commits_behind == 37
    assert report.age_hours is not None and report.age_hours > 0


def test_machines_running_different_commits_are_flagged(fake_git) -> None:
    """Two app machines on different commits is always wrong, never mere lag.

    A checker that read only the first machine would report this as healthy whenever
    that machine happened to be the current one.
    """
    fake_git({("rev-parse", "origin/main"): "abc123"})

    report = build_report(
        "app",
        [_app_machine("abc123"), _app_machine("def456")],
        target_ref="origin/main",
    )

    assert report.divergent_shas == ("abc123", "def456")
    assert report.is_current is False
    assert is_stale(report, max_age_hours=99999) is True


def test_cron_machines_are_ignored(fake_git) -> None:
    """Cron drift is a different check; counting it here would misattribute lag."""
    fake_git({("rev-parse", "origin/main"): "abc123"})

    machines = [
        _app_machine("abc123"),
        _app_machine("staleoldcron", process_group="cron"),
    ]
    report = build_report("app", machines, target_ref="origin/main")

    assert report.divergent_shas == ()
    assert report.is_current is True


def test_missing_sha_label_reports_unknown_rather_than_raising(fake_git) -> None:
    """An older image without the label must not crash the monitor."""
    fake_git({("rev-parse", "origin/main"): "abc123"})

    report = build_report("app", [_app_machine(None)], target_ref="origin/main")

    assert report.deployed_sha is None
    assert report.is_current is False
    assert any("GH_SHA" in note for note in report.notes)


def test_commit_absent_locally_degrades_to_a_note(fake_git) -> None:
    """A shallow clone can't measure distance; it should say so, not explode."""
    fake_git(
        {
            ("rev-parse", "origin/main"): "newsha",
            ("cat-file", "-e", "oldsha^{commit}"): GitError("not found"),
        }
    )

    report = build_report("app", [_app_machine("oldsha")], target_ref="origin/main")

    assert report.commits_behind is None
    assert any("not measurable" in note for note in report.notes)


def test_unknown_age_is_not_treated_as_stale(fake_git) -> None:
    """Absence of evidence must not become a failing alarm.

    An alarm that fires whenever it cannot measure gets muted, and a muted alarm is
    exactly what let #669 run for days.
    """
    fake_git(
        {
            ("rev-parse", "origin/main"): "newsha",
            ("cat-file", "-e", "oldsha^{commit}"): GitError("not found"),
        }
    )

    report = build_report("app", [_app_machine("oldsha")], target_ref="origin/main")

    assert is_stale(report, max_age_hours=1) is False


def test_recent_lag_under_the_threshold_does_not_fail(fake_git) -> None:
    """Same-day lag between merge and deploy is normal, not an incident.

    An alarm that cries wolf daily is one nobody reads.
    """
    from datetime import datetime, timedelta, timezone

    recent = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    fake_git(
        {
            ("rev-parse", "origin/main"): "newsha",
            ("cat-file", "-e", "oldsha^{commit}"): "",
            ("rev-list", "--count", "oldsha..newsha"): "2",
            ("show", "-s", "--format=%cI", "oldsha"): recent,
        }
    )

    report = build_report("app", [_app_machine("oldsha")], target_ref="origin/main")

    assert is_stale(report, max_age_hours=48) is False
    assert is_stale(report, max_age_hours=1) is True


def test_unreachable_flyctl_is_not_reported_as_stale(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """A Fly outage must not masquerade as a stale deployment.

    Conflating "cannot observe" with "observed bad" sends someone to deploy during an
    incident that has nothing to do with the deployment.
    """

    def _boom(_app: str):
        raise freshness_mod.FlyctlError("flyctl executable not found on PATH")

    monkeypatch.setattr(freshness_mod, "_run_flyctl_machine_list", _boom)

    assert main(["--app", "draft-app-prod", "--report-only"]) == 0
    assert "flyctl" in capsys.readouterr().err
