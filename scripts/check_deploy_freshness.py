r"""Report how far a deployed Fly app has fallen behind its source branch.

Failure this descends from
--------------------------
Incident #669. Production ran a 3.5-day-old image through the entire Las Vegas Summer
League window. The fixes for the fault that was actively unfolding -- chunked ingestion
transactions, a bounded Desk writer-lock wait -- were merged to ``main`` and simply not
running: the deployed ``write_lock.py`` was the 26-line version with only the unbounded
acquire, which is why the Desk waited ~95 minutes instead of timing out.

The belief "the chunking fixes shipped" was true of ``main`` and false of production, and
nothing anywhere reported the difference. ``verify_cron_image_digests.py`` catches *cron
machines drifting from the app image*; this catches *the whole app drifting from the
source branch*, which is the axis that was actually unmonitored.

It compounded quietly. Deploying is manual (``workflow_dispatch``), and the post-deploy
digest verifier was itself failing on every attempt, so each deploy ended in a red
workflow -- a standing disincentive to try again. Staleness that nobody measures does not
announce itself; it just grows.

How the deployed commit is known
--------------------------------
``.github/workflows/fly-deploy-*.yml`` stamps ``GH_SHA`` as an image label at build time,
so every running machine carries the exact commit it was built from -- readable from
``flyctl machine list --json`` under ``image_ref.labels``. No extra bookkeeping and
nothing to keep in sync: the label travels with the image.

**Never writes.** Only ``flyctl machine list --json`` and read-only ``git`` queries.

Two failure modes, deliberately distinguished
---------------------------------------------
* **Behind** -- production runs an older commit than the branch. Normal in small amounts;
  a problem when it persists, and the thing #669 turned on.
* **Divergent machines** -- app machines running *different* commits as each other. Always
  wrong, and invisible to a check that only looks at one machine.

Run::

    python scripts/check_deploy_freshness.py --app draft-app-prod
    python scripts/check_deploy_freshness.py --app draft-app-prod --max-age-hours 48
    python scripts/check_deploy_freshness.py --app draft-app-prod --report-only
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.verify_cron_image_digests import (  # noqa: E402
    FlyctlError,
    _run_flyctl_machine_list,
)

logger = logging.getLogger(__name__)

# Deploying same-day is normal; a work-week of drift is the shape #669 had.
DEFAULT_MAX_AGE_HOURS = 48.0

# The image label the deploy workflows stamp with the built commit.
_SHA_LABEL = "GH_SHA"


class GitError(RuntimeError):
    """Raised when a git query needed to measure distance cannot be answered."""


@dataclass(frozen=True)
class FreshnessReport:
    """What the deployed app is running relative to the target branch.

    Attributes:
        app: Fly app name inspected.
        deployed_sha: Commit the running machines were built from, if labelled.
        target_sha: Commit the target ref currently points at.
        commits_behind: How many commits the deployment is missing, if measurable.
        age_hours: Age of the deployed commit in hours, if measurable.
        divergent_shas: Distinct SHAs across app machines when they disagree.
        notes: Why a field is None, when it is.
    """

    app: str
    deployed_sha: Optional[str]
    target_sha: Optional[str]
    commits_behind: Optional[int]
    age_hours: Optional[float]
    divergent_shas: tuple[str, ...]
    notes: tuple[str, ...]

    @property
    def is_current(self) -> bool:
        """True when the deployment matches the target commit exactly."""
        return (
            self.deployed_sha is not None
            and self.deployed_sha == self.target_sha
            and not self.divergent_shas
        )


def _git(*args: str) -> str:
    """Run a read-only git command and return stdout, raising GitError on failure."""
    try:
        result = subprocess.run(
            ["git", *args], capture_output=True, text=True, check=True
        )
    except FileNotFoundError as exc:  # pragma: no cover - git absent
        raise GitError("git executable not found on PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise GitError(f"git {' '.join(args)} failed: {exc.stderr.strip()}") from exc
    return result.stdout.strip()


def _app_machine_shas(machines: list[dict[str, Any]]) -> tuple[list[str], int]:
    """Return app machines' GH_SHA labels and how many app machines lack one.

    Cron machines are excluded on purpose: they are updated on a separate path and
    ``verify_cron_image_digests.py`` already owns that comparison. Mixing them in here
    would report cron lag as app staleness.

    The unlabelled count is returned rather than discarded because dropping those
    machines silently is a way to report CURRENT while part of the fleet runs something
    unknown -- one current machine beside a legacy or half-rolled-out one would pass.
    Only a *fully* unlabelled fleet used to be noticed; a mixed fleet is the dangerous
    case precisely because it looks healthy.

    Returns:
        ``(shas, unlabelled_count)`` over app-process-group machines only.
    """
    shas: list[str] = []
    unlabelled = 0
    for machine in machines:
        config = machine.get("config") or {}
        metadata = config.get("metadata") or {}
        if metadata.get("fly_process_group") != "app":
            continue
        labels = (machine.get("image_ref") or {}).get("labels") or {}
        sha = labels.get(_SHA_LABEL)
        if isinstance(sha, str) and sha:
            shas.append(sha)
        else:
            unlabelled += 1
    return shas, unlabelled


def build_report(
    app: str, machines: list[dict[str, Any]], *, target_ref: str
) -> FreshnessReport:
    """Compare the deployed commit against ``target_ref``.

    Every unmeasurable field degrades to None with a note rather than raising: a
    monitoring check that dies on an unfetched ref reports nothing at all, which is the
    same silence it exists to break.
    """
    notes: list[str] = []
    shas, unlabelled = _app_machine_shas(machines)
    distinct = sorted(set(shas))

    if not shas:
        notes.append(
            f"no app machine carries a {_SHA_LABEL} image label "
            "(image predates the label, or no app-process-group machine exists)"
        )
        deployed: Optional[str] = None
    else:
        # Newest wins for reporting; divergence is reported separately below.
        deployed = shas[0]

    divergent = tuple(distinct) if len(distinct) > 1 else ()
    if divergent:
        notes.append(f"app machines disagree: {', '.join(s[:8] for s in divergent)}")

    # A mixed fleet is treated as divergent, not ignored: an unlabelled machine is
    # running *something*, and "we cannot tell what" is a deployment state to flag
    # rather than to drop on the floor.
    if unlabelled and shas:
        divergent = tuple(sorted({*distinct, "unlabelled"}))
        notes.append(
            f"{unlabelled} app machine(s) carry no {_SHA_LABEL} label while others do; "
            "the fleet is not uniformly identifiable"
        )

    try:
        target: Optional[str] = _git("rev-parse", target_ref)
    except GitError as exc:
        target = None
        notes.append(f"could not resolve {target_ref}: {exc}")

    commits_behind: Optional[int] = None
    age_hours: Optional[float] = None

    if deployed and target:
        try:
            # Fails when the commit is not present locally (shallow clone, unfetched).
            _git("cat-file", "-e", f"{deployed}^{{commit}}")
            commits_behind = int(_git("rev-list", "--count", f"{deployed}..{target}"))
            committed_at = datetime.fromisoformat(
                _git("show", "-s", "--format=%cI", deployed)
            )
            delta = datetime.now(timezone.utc) - committed_at.astimezone(timezone.utc)
            age_hours = round(delta.total_seconds() / 3600, 1)
        except (GitError, ValueError) as exc:
            notes.append(
                f"deployed commit {deployed[:8]} not measurable locally: {exc}"
            )

    return FreshnessReport(
        app=app,
        deployed_sha=deployed,
        target_sha=target,
        commits_behind=commits_behind,
        age_hours=age_hours,
        divergent_shas=divergent,
        notes=tuple(notes),
    )


def format_report(report: FreshnessReport, *, max_age_hours: float) -> str:
    """Render a one-screen human summary."""
    lines = [f"Deploy freshness: {report.app}"]
    lines.append(f"  deployed : {(report.deployed_sha or 'unknown')[:12]}")
    lines.append(f"  target   : {(report.target_sha or 'unknown')[:12]}")

    if report.is_current:
        lines.append("  status   : CURRENT")
    elif report.commits_behind is not None:
        age = f"{report.age_hours}h" if report.age_hours is not None else "unknown age"
        lines.append(
            f"  status   : BEHIND by {report.commits_behind} commit(s), {age} "
            f"(threshold {max_age_hours}h)"
        )
    else:
        lines.append("  status   : UNKNOWN")

    for note in report.notes:
        lines.append(f"  note     : {note}")
    return "\n".join(lines)


def is_stale(report: FreshnessReport, *, max_age_hours: float) -> bool:
    """Whether this report should fail a gating run.

    Divergent machines always fail. Being behind fails only past the age threshold, so
    ordinary same-day lag between a merge and a deploy is not a standing alarm -- an
    alarm that cries wolf daily is one nobody reads, which is how #669 stayed invisible.
    """
    if report.divergent_shas:
        return True
    # A deployment that *is* the target is never stale, however old the commit. Testing
    # age alone made a quiet repository trip the alarm: two days without a merge produced
    # "status: CURRENT" followed by exit 1, and no redeploy could clear it because
    # redeploying the same commit does not make it younger. Age answers "how long has
    # production been behind", which is only a question when it is behind.
    if report.is_current:
        return False
    if report.age_hours is None:
        return False
    return report.age_hours > max_age_hours


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point; returns 1 when stale unless ``--report-only``."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--app", required=True, help="Fly app name, e.g. draft-app-prod"
    )
    parser.add_argument(
        "--against",
        default="origin/main",
        help="Git ref the deployment should match (default: origin/main).",
    )
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=DEFAULT_MAX_AGE_HOURS,
        help=f"Age of the deployed commit that counts as stale (default: {DEFAULT_MAX_AGE_HOURS}).",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Always exit 0. For monitoring that should observe, not gate.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    try:
        machines = _run_flyctl_machine_list(args.app)
    except FlyctlError as exc:
        sys.stderr.write(f"deploy freshness: {exc}\n")
        # Not reachable != stale. Failing here would make an outage look like drift.
        return 0 if args.report_only else 1

    report = build_report(args.app, machines, target_ref=args.against)
    print(format_report(report, max_age_hours=args.max_age_hours))

    if args.report_only or not is_stale(report, max_age_hours=args.max_age_hours):
        return 0

    sys.stderr.write(
        "\n".join(
            [
                "",
                f"ERROR: {args.app} is running stale code.",
                "",
                "Production ran 3.5 days behind main through the entire Summer League",
                "window in incident #669, and nothing reported it. Deploy, or raise",
                "--max-age-hours deliberately.",
                "",
            ]
        )
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
