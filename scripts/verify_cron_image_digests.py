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

Also importable as a routine, non-deploy-gated monitoring check
(:func:`run_monitoring_check`, or ``--report-only`` from the CLI): unlike
:func:`verify_cron_image_digests` (deploy-gating -- returns just the
mismatched names, and the CLI's default mode exits 1 on drift),
:func:`run_monitoring_check` always completes and logs one structured record
per machine via the module logger, so a periodic monitoring cron can run this
same digest check on a schedule and have drift show up as a greppable log
line without failing the job that observed it.

Run:
  python scripts/verify_cron_image_digests.py --app draft-app-prod
  python scripts/verify_cron_image_digests.py --app draft-app-prod \\
      --machine summer-league-desk-cron --optional-machine summer-league-desk-cron
  python scripts/verify_cron_image_digests.py --app draft-app-prod --report-only
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Optional, Sequence

logger = logging.getLogger(__name__)

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

    current_digest: Optional[str] = None
    expected_digest: Optional[str] = None

    @property
    def matches(self) -> bool:
        """Whether the machine exists and its image matches the deployed app image.

        Compares content digests when both sides report one -- the strictest and
        most literal reading of "same image", and what this script is named for.

        Falls back to comparing *normalized* references only when a digest is
        missing on either side. Raw string equality is wrong here: Fly reports an
        app machine's ``config.image`` as ``repo:tag`` but pins cron machines to
        ``repo:tag@sha256:...`` once updated, so identical images compared as
        strings never match. That false positive failed every prod deploy at the
        post-deploy gate from 2026-07-23 onward.
        """
        if not self.found:
            return False
        if self.current_digest and self.expected_digest:
            return self.current_digest == self.expected_digest
        if self.current_image is None or self.expected_image is None:
            return False
        return _normalize_image_ref(self.current_image) == _normalize_image_ref(
            self.expected_image
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


def _normalize_image_ref(image: str) -> str:
    """Strip a ``@sha256:...`` suffix so ``repo:tag`` references compare equal.

    Only used when a content digest is unavailable on one side of a comparison.
    Fly deployment tags (``deployment-01KY...``) are unique per deploy, so the
    tag alone is still a sound identity check -- just a weaker one than a digest.
    """
    return image.split("@", 1)[0]


def _machine_digest(machine: dict[str, Any]) -> Optional[str]:
    """Return a machine's image content digest from ``image_ref``, if reported.

    Fly exposes the resolved digest on every machine as ``image_ref.digest``,
    including app machines whose ``config.image`` carries only a tag. Preferring
    it is what makes this a digest check rather than a string check.
    """
    image_ref = machine.get("image_ref") or {}
    digest = image_ref.get("digest")
    return digest if isinstance(digest, str) and digest else None


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


def _resolve_app_digest(machines: list[dict[str, Any]]) -> Optional[str]:
    """Return the deployed app machine's image digest, if Fly reports one."""
    for machine in machines:
        config = machine.get("config") or {}
        metadata = config.get("metadata") or {}
        if metadata.get("fly_process_group") == "app":
            return _machine_digest(machine)
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
    expected_digest = _resolve_app_digest(machines)
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
                    expected_digest=expected_digest,
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
                current_digest=_machine_digest(resolved),
                expected_digest=expected_digest,
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


def _log_machine_status(status: MachineImageStatus) -> None:
    """Emit one structured log record for a machine's digest comparison.

    Mirrors ``PipelineTelemetry.step``'s "one structured log record per
    observation" philosophy (see
    ``app.services.summer_league.pipeline_telemetry``), but this script runs
    as a standalone ``flyctl``-shelling CLI outside the async DB pipeline --
    no ``AsyncSession``, no pipeline ``job``/``run_id`` -- so it cannot
    import and use ``PipelineTelemetry`` directly. This is the comparable
    structured line for that context: greppable ``key=value`` fields, one
    record per machine, logged through the standard ``logging`` module
    (rather than ``print``, which :func:`main`'s CLI report continues to use
    for the human-readable summary) so it composes with whatever log
    aggregation the calling cron already has.
    """
    outcome = (
        "matched" if status.matches else ("missing" if not status.found else "drift")
    )
    logger.info(
        "summer_league_cron_image_status machine=%s outcome=%s "
        "current_image=%s expected_image=%s",
        status.name,
        outcome,
        status.current_image,
        status.expected_image,
    )


def run_monitoring_check(
    app: str, expected_machine_names: Optional[Sequence[str]] = None
) -> list[MachineImageStatus]:
    """Non-failing routine check: log every machine's current-vs-desired digest.

    Unlike :func:`verify_cron_image_digests` (deploy-gating -- returns only
    the mismatched/missing names, for a caller that wants to fail a deploy
    on drift), this is meant for a periodic monitoring cron: it logs one
    structured record (:func:`_log_machine_status`) per machine -- matched
    ones included, not just drifted ones, since routine monitoring wants
    full visibility -- and never raises or signals failure over drift
    itself. Only an actual inability to query Fly (:class:`FlyctlError`)
    propagates; that is a real operational problem this check cannot paper
    over.

    Args:
        app: Fly app name, e.g. ``"draft-app-prod"``.
        expected_machine_names: Cron machine names to check; defaults to
            :data:`DEFAULT_SUMMER_LEAGUE_CRON_MACHINES`.

    Returns:
        Every machine's :class:`MachineImageStatus`, for a caller that wants
        to do more than log (e.g. push to a dashboard/metrics sink).

    Raises:
        FlyctlError: if ``flyctl machine list`` cannot be run or its output
            can't be parsed.
    """
    names = expected_machine_names or DEFAULT_SUMMER_LEAGUE_CRON_MACHINES
    machines = _run_flyctl_machine_list(app)
    statuses = build_machine_statuses(machines, names)
    for status in statuses:
        _log_machine_status(status)
    return statuses


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
    parser.add_argument(
        "--report-only",
        action="store_true",
        help=(
            "Run as a non-failing routine monitoring check (see "
            "run_monitoring_check): log every machine's current-vs-desired "
            "digest and always exit 0, even on drift. For a periodic "
            "monitoring cron -- the default (gating) mode below still exits "
            "1 on drift and remains the right choice for post-deploy CI."
        ),
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Parse args, run the verification, print a report, and return its exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    expected_machine_names = args.machines or list(DEFAULT_SUMMER_LEAGUE_CRON_MACHINES)
    optional_machines = set(args.optional_machines)

    if args.report_only:
        try:
            statuses = run_monitoring_check(args.app, expected_machine_names)
        except FlyctlError as exc:
            print(f"::error::{exc}", flush=True)
            return 1
        print(_format_report(statuses), flush=True)
        return 0

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
