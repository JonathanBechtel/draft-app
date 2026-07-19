r"""Post-deploy verification that Summer League cron machines run the current image.

Background
----------
``.github/workflows/fly-deploy-prod.yml`` updates each Summer League cron
machine's image after every deploy, but the Desk cron update is best-effort:
if the machine does not stop within its wait window (because a tick is in
flight), the workflow logs ``::warning::...skipping its image update`` and
moves on. Nothing else in the deploy notices when that happens, so a Desk
cron machine can silently keep running stale code for days.

This script is the gate that closes that hole: given a Fly app and a list of
expected cron machine names, it fetches the current machine list, resolves
the just-deployed app image (the same "app" process-group machine every
cron-update step in the workflow already reads), and reports which named
machines are missing or running a different image. Called from CI as the
final post-deploy step so drift becomes a failed check instead of something
a human has to notice by manually diffing machine images.

**Never writes.** Only calls ``flyctl machine list --json`` (a read).

Run:
  python scripts/verify_cron_image_digests.py --app draft-app-prod
  python scripts/verify_cron_image_digests.py --app draft-app-prod \\
      --machine summer-league-desk-cron --optional-machine summer-league-desk-cron
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Optional, Sequence

# The three Summer League cron machines this ticket's spec names explicitly.
# `summer-league-desk-cron` is readiness-gated (#536) and may legitimately not
# exist yet in an environment that has never promoted the Desk cron to prod,
# so callers that want to tolerate its absence should pass it via
# `--optional-machine` rather than omitting it -- an environment where it
# *does* exist but drifted must still fail.
DEFAULT_SUMMER_LEAGUE_CRON_MACHINES: tuple[str, ...] = (
    "summer-league-ingestion-cron",
    "summer-league-desk-cron",
    "summer-league-roster-cron",
)


class FlyctlError(RuntimeError):
    """Raised when ``flyctl machine list`` cannot be run or its output parsed."""


@dataclass(frozen=True)
class MachineImageStatus:
    """One machine's image-digest comparison result."""

    name: str
    found: bool
    current_image: Optional[str]
    expected_image: Optional[str]

    @property
    def matches(self) -> bool:
        """Whether the machine exists and its image matches the deployed app image."""
        return (
            self.found
            and self.current_image is not None
            and self.expected_image is not None
            and self.current_image == self.expected_image
        )


def _run_flyctl_machine_list(app: str) -> list[dict[str, Any]]:
    """Invoke ``flyctl machine list --app <app> --json`` and parse its output.

    Args:
        app: Fly app name, e.g. ``"draft-app-prod"``.

    Returns:
        The parsed JSON array of machine objects.

    Raises:
        FlyctlError: if flyctl is missing, exits non-zero, or emits unparseable JSON.
    """
    try:
        result = subprocess.run(
            ["flyctl", "machine", "list", "--app", app, "--json"],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise FlyctlError("flyctl executable not found on PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise FlyctlError(
            f"flyctl machine list --app {app} --json failed "
            f"(exit {exc.returncode}): {exc.stderr}"
        ) from exc

    try:
        machines = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise FlyctlError(
            f"Could not parse flyctl machine list output as JSON: {exc}"
        ) from exc

    if not isinstance(machines, list):
        raise FlyctlError(
            f"Unexpected flyctl machine list output shape: {type(machines)!r}"
        )
    return machines


def _resolve_app_image(machines: list[dict[str, Any]]) -> Optional[str]:
    """Find the just-deployed app image from the ``app`` process-group machine.

    Mirrors the ``jq`` expression every cron-update step in
    ``fly-deploy-prod.yml`` already uses:
    ``select(.config.metadata.fly_process_group == "app") | first | .config.image``.
    """
    for machine in machines:
        config = machine.get("config") or {}
        metadata = config.get("metadata") or {}
        if metadata.get("fly_process_group") == "app":
            image = config.get("image")
            return image if isinstance(image, str) else None
    return None


def build_machine_statuses(
    machines: list[dict[str, Any]], expected_machine_names: Sequence[str]
) -> list[MachineImageStatus]:
    """Compare each expected machine's image against the deployed app image.

    Args:
        machines: Parsed ``flyctl machine list --json`` output.
        expected_machine_names: Cron machine names to check, in report order.

    Returns:
        One :class:`MachineImageStatus` per name in ``expected_machine_names``.
    """
    expected_image = _resolve_app_image(machines)
    by_name: dict[str, dict[str, Any]] = {}
    for machine in machines:
        name = machine.get("name")
        if isinstance(name, str):
            by_name[name] = machine

    statuses: list[MachineImageStatus] = []
    for name in expected_machine_names:
        resolved = by_name.get(name)
        if resolved is None:
            statuses.append(
                MachineImageStatus(
                    name=name,
                    found=False,
                    current_image=None,
                    expected_image=expected_image,
                )
            )
            continue
        config = resolved.get("config") or {}
        current_image = config.get("image")
        statuses.append(
            MachineImageStatus(
                name=name,
                found=True,
                current_image=current_image if isinstance(current_image, str) else None,
                expected_image=expected_image,
            )
        )
    return statuses


def verify_cron_image_digests(
    app: str, expected_machine_names: Sequence[str]
) -> list[str]:
    """Return machine names whose image digest doesn't match the app image.

    A machine counts as not matching if it is missing entirely, if its image
    could not be determined, or if its image differs from the deployed app
    image. Empty when every named machine is present and matches.

    Args:
        app: Fly app name, e.g. ``"draft-app-prod"``.
        expected_machine_names: Cron machine names that must be on the current
            app image.

    Returns:
        Names (subset of ``expected_machine_names``, in order) that don't match.

    Raises:
        FlyctlError: if ``flyctl machine list`` cannot be run or its output
            can't be parsed.
    """
    machines = _run_flyctl_machine_list(app)
    statuses = build_machine_statuses(machines, expected_machine_names)
    return [status.name for status in statuses if not status.matches]


def _format_report(statuses: list[MachineImageStatus]) -> str:
    """Human-readable, CI-log-friendly rendering of the per-machine statuses."""
    lines = ["Summer League cron image digest verification:"]
    for status in statuses:
        if not status.found:
            lines.append(f"  [MISSING] {status.name}: machine not found")
        elif status.current_image is None:
            lines.append(
                f"  [UNKNOWN] {status.name}: could not determine current image"
            )
        elif status.matches:
            lines.append(f"  [OK]      {status.name}: {status.current_image}")
        else:
            lines.append(
                f"  [DRIFT]   {status.name}: current={status.current_image} "
                f"expected={status.expected_image}"
            )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--app", required=True, help="Fly app name, e.g. draft-app-prod"
    )
    parser.add_argument(
        "--machine",
        dest="machines",
        action="append",
        default=None,
        help=(
            "Cron machine name to verify; repeatable. Defaults to the three "
            "Summer League cron machines if omitted."
        ),
    )
    parser.add_argument(
        "--optional-machine",
        dest="optional_machines",
        action="append",
        default=[],
        help=(
            "A machine name (must also be passed via --machine, or be one of "
            "the defaults) whose complete *absence* should not fail the check "
            "-- e.g. a readiness-gated cron not yet promoted to this "
            "environment. Drift on a machine that DOES exist still fails "
            "regardless of this flag."
        ),
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Parse args, run the verification, print a report, and return its exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    expected_machine_names = args.machines or list(DEFAULT_SUMMER_LEAGUE_CRON_MACHINES)
    optional_machines = set(args.optional_machines)

    try:
        machines = _run_flyctl_machine_list(args.app)
    except FlyctlError as exc:
        print(f"::error::{exc}", flush=True)
        return 1

    statuses = build_machine_statuses(machines, expected_machine_names)
    print(_format_report(statuses), flush=True)

    hard_failures = [
        status.name
        for status in statuses
        if not status.matches
        and not (status.found is False and status.name in optional_machines)
    ]
    if hard_failures:
        print(
            f"::error::Summer League cron image drift detected for: "
            f"{', '.join(hard_failures)}. Re-run the deploy's cron-update steps "
            "or manually `flyctl machine update` the listed machine(s) to the "
            "current app image, then re-run this check.",
            flush=True,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
