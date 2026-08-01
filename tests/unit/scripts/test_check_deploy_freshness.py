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

import subprocess
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


def test_a_current_deployment_is_never_stale_however_old_the_commit(fake_git) -> None:
    """A quiet repository must not trip the alarm.

    Testing age alone made two days without a merge produce "status: CURRENT" followed
    by exit 1 -- and no redeploy could clear it, because redeploying the same commit
    does not make it younger. Age answers "how long has production been behind", which
    is only a question when it is behind.
    """
    fake_git(
        {
            ("rev-parse", "origin/main"): "abc123",
            ("cat-file", "-e", "abc123^{commit}"): "",
            ("rev-list", "--count", "abc123..abc123"): "0",
            ("show", "-s", "--format=%cI", "abc123"): "2026-01-01T00:00:00+00:00",
        }
    )

    report = build_report("app", [_app_machine("abc123")], target_ref="origin/main")

    assert report.is_current is True
    assert is_stale(report, max_age_hours=48) is False


def test_a_partially_unlabelled_fleet_is_flagged(fake_git) -> None:
    """One labelled machine on main beside an unlabelled one is not "CURRENT".

    Silently dropping the unlabelled machine let a mixed fleet -- a current machine
    next to a legacy or half-rolled-out one -- report healthy. Only a *fully*
    unlabelled fleet was noticed before, which is the less dangerous case.
    """
    fake_git({("rev-parse", "origin/main"): "abc123"})

    report = build_report(
        "app",
        [_app_machine("abc123"), _app_machine(None)],
        target_ref="origin/main",
    )

    assert report.is_current is False
    assert "unlabelled" in report.divergent_shas
    assert is_stale(report, max_age_hours=99999) is True


def test_measures_distance_from_the_branch(fake_git) -> None:
    """The #669 shape: production behind main, with the gap quantified."""
    fake_git(
        {
            ("rev-parse", "origin/main"): "newsha",
            ("cat-file", "-e", "oldsha^{commit}"): "",
            ("rev-list", "--count", "oldsha..newsha"): "37",
            (
                "rev-list",
                "--reverse",
                "--max-count=1",
                "oldsha..newsha",
            ): "firstmissing",
            ("show", "-s", "--format=%cI", "firstmissing"): "2026-07-19T22:39:00+00:00",
        }
    )

    report = build_report("app", [_app_machine("oldsha")], target_ref="origin/main")

    assert report.is_current is False
    assert report.commits_behind == 37
    assert report.age_hours is not None and report.age_hours > 0


def test_age_hours_measures_time_since_the_first_missing_commit(fake_git) -> None:
    """Age is time-behind, not the deployed commit's own committer-age.

    Scoring the deployed commit's own age instead cries wolf after a quiet week on
    main: a deployment one commit behind after a week of silence would read that
    commit as "old" even though nothing has actually been waiting on it. The oldest
    *missing* commit's landing time is the thing that answers "how long has this
    fix been sitting undeployed".
    """
    fake_git(
        {
            ("rev-parse", "origin/main"): "newsha",
            ("cat-file", "-e", "oldsha^{commit}"): "",
            ("rev-list", "--count", "oldsha..newsha"): "1",
            ("rev-list", "--reverse", "--max-count=1", "oldsha..newsha"): "newsha",
            # The deployed commit itself is ancient (a quiet week), but the single
            # missing commit landed recently -- age_hours must track the latter.
            ("show", "-s", "--format=%cI", "oldsha"): "2020-01-01T00:00:00+00:00",
            ("show", "-s", "--format=%cI", "newsha"): "2026-07-19T22:39:00+00:00",
        }
    )

    report = build_report("app", [_app_machine("oldsha")], target_ref="origin/main")

    assert report.age_hours is not None
    # If age_hours were measuring the deployed commit's own age it would be years,
    # not the few days since 2026-07-19.
    assert report.age_hours < 24 * 30


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


def test_a_fully_unlabelled_fleet_fails_the_gating_check(fake_git) -> None:
    """UNKNOWN must fail, not degrade to permanent success.

    A manual `flyctl deploy` from a checkout (the documented Jul 6 incident deploy
    path) never stamps GH_SHA, so a fully unlabelled fleet used to report UNKNOWN and
    return "not stale" here -- exiting 0 indefinitely on exactly the deploy path most
    likely to cause drift.
    """
    fake_git({("rev-parse", "origin/main"): "abc123"})

    report = build_report("app", [_app_machine(None)], target_ref="origin/main")

    assert report.deployed_sha is None
    assert is_stale(report, max_age_hours=99999) is True


def test_report_only_is_the_escape_hatch_for_a_deliberately_unlabelled_fleet(
    fake_git, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """--report-only exits 0 even when the fleet is fully unlabelled.

    The failure mode this fixes is a fleet unlabelled by accident; a fleet that is
    unlabelled on purpose (or during a migration) needs a way to keep observing
    without gating a CI run red every day.
    """
    fake_git({("rev-parse", "origin/main"): "abc123"})
    monkeypatch.setattr(
        freshness_mod,
        "_run_flyctl_machine_list",
        lambda _app: [_app_machine(None)],
    )

    exit_code = main(["--app", "draft-app-prod", "--report-only"])

    assert exit_code == 0
    assert "UNKNOWN" in capsys.readouterr().out


def test_a_fully_unlabelled_fleet_fails_main_with_a_targeted_message(
    fake_git, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The CLI's error message names the unlabelled-fleet cause, not just "stale"."""
    fake_git({("rev-parse", "origin/main"): "abc123"})
    monkeypatch.setattr(
        freshness_mod,
        "_run_flyctl_machine_list",
        lambda _app: [_app_machine(None)],
    )

    exit_code = main(["--app", "draft-app-prod"])

    assert exit_code == 1
    assert "GH_SHA" in capsys.readouterr().err


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
            (
                "rev-list",
                "--reverse",
                "--max-count=1",
                "oldsha..newsha",
            ): "firstmissing",
            ("show", "-s", "--format=%cI", "firstmissing"): recent,
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


def _stale_fleet_git() -> dict[tuple[str, ...], str]:
    """Git responses describing a fleet one old commit behind the target ref."""
    return {
        ("rev-parse", "origin/main"): "newsha",
        ("cat-file", "-e", "oldsha^{commit}"): "",
        ("rev-list", "--count", "oldsha..newsha"): "1",
        ("rev-list", "--reverse", "--max-count=1", "oldsha..newsha"): "newsha",
        ("show", "-s", "--format=%cI", "oldsha"): "2020-01-01T00:00:00+00:00",
        ("show", "-s", "--format=%cI", "newsha"): "2020-01-02T00:00:00+00:00",
    }


def test_main_fails_with_the_stale_code_message_when_over_the_age_threshold(
    fake_git, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The checker's primary alarm: BEHIND and too old exits 1 with the #669 message.

    This is the branch the whole script exists for, and it is only reachable
    through ``main()``'s real argv parsing -- calling ``is_stale`` directly with
    a Python float leaves ``--max-age-hours`` untested, so a wrong ``type=`` or
    a value never threaded into ``is_stale`` would silence the alarm with a
    fully green suite.
    """
    fake_git(_stale_fleet_git())
    monkeypatch.setattr(
        freshness_mod,
        "_run_flyctl_machine_list",
        lambda _app: [_app_machine("oldsha")],
    )

    exit_code = main(["--app", "draft-app-prod", "--max-age-hours", "1"])

    assert exit_code == 1
    assert "running stale code" in capsys.readouterr().err


def test_main_passes_when_the_age_threshold_is_raised_above_the_lag(
    fake_git, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The same fleet under a permissive threshold exits 0.

    Paired with the test above, this proves ``--max-age-hours`` is genuinely
    read from argv and drives the decision, rather than the exit code being
    fixed by the fleet's shape alone.
    """
    fake_git(_stale_fleet_git())
    monkeypatch.setattr(
        freshness_mod,
        "_run_flyctl_machine_list",
        lambda _app: [_app_machine("oldsha")],
    )

    exit_code = main(["--app", "draft-app-prod", "--max-age-hours", "999999"])

    assert exit_code == 0
    assert "running stale code" not in capsys.readouterr().err


def test_main_compares_against_the_ref_named_by_the_against_flag(
    fake_git, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--against`` must select the ref the deployment is compared to.

    The ``fake_git`` stub raises ``GitError`` on any unexpected git call, so a
    ``--against`` value that never reached ``build_report`` would resolve
    ``origin/main`` instead, produce a "could not resolve" note, and fail the
    exit-0 assertion below.
    """
    fake_git({("rev-parse", "refs/heads/release"): "abc123"})
    monkeypatch.setattr(
        freshness_mod,
        "_run_flyctl_machine_list",
        lambda _app: [_app_machine("abc123")],
    )

    exit_code = main(["--app", "draft-app-prod", "--against", "refs/heads/release"])

    assert exit_code == 0


def test_an_unresolvable_target_ref_degrades_to_a_note(fake_git) -> None:
    """A ref that git cannot resolve must be reported, not raised.

    ``build_report`` catches ``GitError`` from the ``rev-parse`` so a typo'd or
    not-yet-fetched ref produces an observable note instead of killing the
    monitor -- the same degrade-don't-die posture as the unknown-age path.
    """
    fake_git({})

    report = build_report(
        "app", [_app_machine("oldsha")], target_ref="origin/does-not-exist"
    )

    assert report.target_sha is None
    assert any("could not resolve" in note for note in report.notes)


def test_git_failures_are_translated_into_giterror_with_the_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real subprocess wrapper must surface git's own stderr in the error.

    Every other test stubs ``_git`` itself, so without this the module's only
    real process boundary -- and the message an operator would actually read
    when the check breaks -- is never exercised.
    """

    def _failing_run(*_args: object, **_kwargs: object):
        raise subprocess.CalledProcessError(
            returncode=128,
            cmd=["git", "rev-parse", "nope"],
            stderr="fatal: bad revision",
        )

    monkeypatch.setattr(freshness_mod.subprocess, "run", _failing_run)

    with pytest.raises(GitError) as excinfo:
        freshness_mod._git("rev-parse", "nope")

    assert "fatal: bad revision" in str(excinfo.value)
