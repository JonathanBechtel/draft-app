r"""Second-chance retry for a Fly cron machine's stop-wait-timeout image update.

Background
----------
``.github/workflows/fly-deploy-prod.yml``'s "Update Summer League Desk cron
machine" step waits up to 30 minutes for that machine to stop before applying
`flyctl machine update` (a running scheduled machine gets sent SIGINT by
`machine update`, which the workflow deliberately avoids interrupting). If the
wait times out, the step used to just log a warning and move on -- leaving the
machine on the old image with no later reconciliation and no way for anyone to
notice short of manually diffing machine images.

This script is that later reconciliation: given a machine that failed its
deploy-time stop-wait, poll (with its own bounded timeout) for it to reach
`stopped`, then apply the same `machine update --skip-start` + `machine start`
sequence the original step uses -- re-arming the Fly schedule exactly like the
original wait-then-update block does (see the comment there for why `machine
start` is required after `machine update` leaves a scheduled machine stopped).

**Idempotent / safe to call speculatively.** If the machine is already on the
current image, this is a no-op (`ALREADY_CURRENT`). If it isn't found at all,
it reports `MACHINE_NOT_FOUND` rather than erroring -- there may be nothing to
reconcile in some environments (e.g. a readiness-gated cron never promoted).

Run:
  python scripts/reconcile_cron_image.py --app draft-app-prod \\
      --machine summer-league-desk-cron --wait-timeout-s 600
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Optional, Protocol, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.verify_cron_image_digests import (  # noqa: E402
    FlyctlError,
    _resolve_app_image,
    _run_flyctl_machine_list,
)


class _CompletedProcessLike(Protocol):
    """The subset of `subprocess.CompletedProcess` this module relies on.

    A structural (not nominal) type so tests can pass a lightweight fake
    process result without subclassing `subprocess.CompletedProcess`.
    """

    returncode: int
    stderr: str


RunFn = Callable[[Sequence[str]], _CompletedProcessLike]


class ReconcileOutcome(str, Enum):
    """What happened when reconciling one machine's image."""

    UPDATED = "updated"
    ALREADY_CURRENT = "already_current"
    MACHINE_NOT_FOUND = "machine_not_found"
    STILL_RUNNING = "still_running"
    UPDATE_FAILED = "update_failed"
    START_FAILED = "start_failed"


@dataclass(frozen=True)
class ReconcileResult:
    """One machine's reconciliation outcome."""

    outcome: ReconcileOutcome
    machine_name: str
    message: str

    @property
    def ok(self) -> bool:
        """Whether the machine is on the current image (already, or after this run)."""
        return self.outcome in (
            ReconcileOutcome.UPDATED,
            ReconcileOutcome.ALREADY_CURRENT,
            ReconcileOutcome.MACHINE_NOT_FOUND,
        )


def _default_run(args: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    """Default ``run`` implementation: shell out to the real ``flyctl``."""
    return subprocess.run(list(args), capture_output=True, text=True)


def reconcile_cron_machine_image(
    app: str,
    machine_name: str,
    *,
    wait_timeout_s: int = 600,
    run: Optional[RunFn] = None,
) -> ReconcileResult:
    """Wait for a stopped-scheduled cron machine, then update+re-arm its image.

    Mirrors the deploy workflow's existing wait -> update --skip-start -> start
    sequence for a named cron machine, as a bounded second chance for the case
    where the deploy-time wait already timed out once (or as a general
    "make sure this machine is current" idempotent check).

    Args:
        app: Fly app name, e.g. ``"draft-app-prod"``.
        machine_name: The cron machine's ``name``, e.g. ``"summer-league-desk-cron"``.
        wait_timeout_s: Bounded wait (seconds) for the machine to reach `stopped`
            before giving up on this retry. Deliberately short relative to the
            deploy-time 30-minute wait -- this is a follow-up chance, not
            another full wait.
        run: Injectable subprocess runner (tests pass a fake).

    Returns:
        A :class:`ReconcileResult` describing what happened.

    Raises:
        FlyctlError: if ``flyctl machine list`` cannot be run or its output
            can't be parsed.
    """
    run = run if run is not None else _default_run
    machines = _run_flyctl_machine_list(app)
    app_image = _resolve_app_image(machines)
    by_name = {
        machine.get("name"): machine
        for machine in machines
        if isinstance(machine.get("name"), str)
    }
    machine = by_name.get(machine_name)
    if machine is None:
        return ReconcileResult(
            outcome=ReconcileOutcome.MACHINE_NOT_FOUND,
            machine_name=machine_name,
            message=f"No machine named {machine_name!r} found for app {app!r}.",
        )

    machine_id = machine.get("id")
    current_image = (machine.get("config") or {}).get("image")
    if app_image is not None and current_image == app_image:
        return ReconcileResult(
            outcome=ReconcileOutcome.ALREADY_CURRENT,
            machine_name=machine_name,
            message=f"{machine_name} already on current image {app_image}.",
        )

    wait_result = run(
        [
            "flyctl",
            "machine",
            "wait",
            str(machine_id),
            "--app",
            app,
            "--state",
            "stopped",
            "--wait-timeout",
            f"{wait_timeout_s}s",
        ]
    )
    if wait_result.returncode != 0:
        return ReconcileResult(
            outcome=ReconcileOutcome.STILL_RUNNING,
            machine_name=machine_name,
            message=(
                f"{machine_name} did not stop within {wait_timeout_s}s on retry; "
                "leaving it on its current image. A later deploy's retry (or a "
                "manual `flyctl machine update`) must reconcile it."
            ),
        )

    update_result = run(
        [
            "flyctl",
            "machine",
            "update",
            str(machine_id),
            "--app",
            app,
            "--image",
            str(app_image),
            "--skip-start",
            "--yes",
        ]
    )
    if update_result.returncode != 0:
        return ReconcileResult(
            outcome=ReconcileOutcome.UPDATE_FAILED,
            machine_name=machine_name,
            message=(
                f"flyctl machine update failed for {machine_name}: "
                f"{update_result.stderr}"
            ),
        )

    # `machine update` does not reliably re-arm Fly's schedule for a scheduled
    # machine left stopped -- see the identical comment in fly-deploy-prod.yml's
    # original Desk cron update step. Starting it here both kicks off an
    # immediate tick on the new image and re-arms the schedule.
    start_result = run(["flyctl", "machine", "start", str(machine_id), "--app", app])
    if start_result.returncode != 0:
        return ReconcileResult(
            outcome=ReconcileOutcome.START_FAILED,
            machine_name=machine_name,
            message=(
                f"Updated {machine_name}'s image but `flyctl machine start` failed "
                f"({start_result.stderr}); its schedule may not be re-armed."
            ),
        )

    return ReconcileResult(
        outcome=ReconcileOutcome.UPDATED,
        machine_name=machine_name,
        message=f"Updated {machine_name} to {app_image} and re-armed its schedule.",
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--app", required=True, help="Fly app name, e.g. draft-app-prod"
    )
    parser.add_argument(
        "--machine", required=True, help="Cron machine name to reconcile"
    )
    parser.add_argument(
        "--wait-timeout-s",
        type=int,
        default=600,
        help="Bounded wait (seconds) for the machine to stop before giving up (default: 600).",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Parse args, attempt reconciliation, print the outcome, and return its exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        result = reconcile_cron_machine_image(
            args.app, args.machine, wait_timeout_s=args.wait_timeout_s
        )
    except FlyctlError as exc:
        print(f"::error::{exc}", flush=True)
        return 1

    print(result.message, flush=True)
    if not result.ok:
        print(
            f"::error::{result.machine_name} could not be reconciled onto the "
            "current image within this retry window.",
            flush=True,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
