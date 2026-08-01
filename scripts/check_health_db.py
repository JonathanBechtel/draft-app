r"""Probe ``/health/db`` on deployed apps and fail when the database is unreachable.

Failure this closes
--------------------
PR #684 added ``/health/db`` -- a readiness probe that runs a bounded ``SELECT 1``
through the app's own pool and reports 503 when it fails, specifically so incident
#669's shape (``/health`` green, DB-backed routes 500ing for ~96 minutes, nothing
reporting it) would be visible next time. But wiring it to anything was left as an
operator step: ``docs/fly_infrastructure.md`` says to point *external* uptime
monitoring at it, and until someone does that outside this repo, the endpoint exists
and nothing is listening. "The signal exists" and "someone is watching it" are not the
same fact, and only the second one catches an outage.

This script is the cheapest in-repo closing of that gap: it is not a substitute for
real uptime monitoring (no retries, no paging, no history), just the same posture as
``check_deploy_freshness.py`` -- a scheduled GitHub Actions job that turns a real
outage into a red run someone will see the same day, without requiring any external
service to be provisioned first.

**Never writes.** Only unauthenticated ``GET`` requests to a public health endpoint.

Run::

    python scripts/check_health_db.py --url https://draft-app.fly.dev
    python scripts/check_health_db.py \\
        --url https://draft-app.fly.dev --url https://draft-app-prod.fly.dev
    python scripts/check_health_db.py --url https://draft-app-prod.fly.dev --report-only
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from typing import Callable, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

# Bounded well past the endpoint's own 5s internal timeout (see app/main.py's
# database_health_check docstring) so a slow probe reports "the app took too long",
# not "the network hung forever".
DEFAULT_TIMEOUT_S = 15.0

HEALTH_PATH = "/health/db"

# (status_code, response_body) -- injected so tests never touch the network.
FetchFunc = Callable[[str, float], "tuple[int, bytes]"]


@dataclass(frozen=True)
class HealthProbeResult:
    """One app's ``/health/db`` probe outcome.

    Attributes:
        url: The full health-check URL that was probed.
        ok: True when the probe reached the app and got HTTP 200.
        status_code: The HTTP status returned, if the app was reachable at all.
        detail: Human-readable summary -- "ok", an HTTP status, or an error string.
    """

    url: str
    ok: bool
    status_code: Optional[int]
    detail: str


def _default_fetch(url: str, timeout_s: float) -> "tuple[int, bytes]":
    """Perform the real HTTP GET.

    ``urlopen`` raises ``HTTPError`` for non-2xx responses rather than returning one,
    but ``HTTPError`` is itself a readable response object -- its status and body are
    extracted the same way a plain success would be, so a 503 reads as data, not an
    exception the caller has to specifically expect.
    """
    try:
        with urlopen(url, timeout=timeout_s) as resp:  # noqa: S310 - fixed https URLs
            return resp.status, resp.read()
    except HTTPError as exc:
        return exc.code, exc.read()


def probe_one(
    app_url: str,
    *,
    fetch: FetchFunc = _default_fetch,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> HealthProbeResult:
    """Probe a single app's ``/health/db`` endpoint.

    Args:
        app_url: Base app URL, e.g. ``"https://draft-app-prod.fly.dev"``. Any
            trailing slash is stripped before appending ``/health/db``.
        fetch: HTTP GET implementation; defaults to a real request. Tests inject a
            stub so this module never touches the network.
        timeout_s: Per-request timeout in seconds.

    Returns:
        A :class:`HealthProbeResult`. Never raises -- a probe that crashes the
        monitor produces the same silence #669 did, so every failure mode (timeout,
        DNS failure, non-200, unreadable body) degrades to ``ok=False`` with a note
        rather than propagating.
    """
    url = app_url.rstrip("/") + HEALTH_PATH
    try:
        status, body = fetch(url, timeout_s)
    except URLError as exc:
        return HealthProbeResult(
            url=url, ok=False, status_code=None, detail=f"unreachable: {exc.reason}"
        )
    except Exception as exc:  # defensive: a probe must never crash the monitor
        return HealthProbeResult(
            url=url, ok=False, status_code=None, detail=f"error: {exc}"
        )

    if status == 200:
        return HealthProbeResult(url=url, ok=True, status_code=status, detail="ok")

    detail = f"HTTP {status}"
    try:
        payload = json.loads(body)
        if isinstance(payload, dict) and payload.get("error"):
            detail = f"{detail}: {payload['error']}"
    except (ValueError, TypeError):
        pass
    return HealthProbeResult(url=url, ok=False, status_code=status, detail=detail)


def format_report(results: Sequence[HealthProbeResult]) -> str:
    """Render a one-screen human summary."""
    lines = [f"Database health probe ({HEALTH_PATH}):"]
    for result in results:
        status = "OK" if result.ok else "FAIL"
        lines.append(f"  [{status}] {result.url}: {result.detail}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point; returns 1 if any probed app is unhealthy, unless ``--report-only``."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        dest="urls",
        action="append",
        required=True,
        help=(
            "Base app URL to probe, e.g. https://draft-app-prod.fly.dev; "
            "repeatable for multiple apps."
        ),
    )
    parser.add_argument(
        "--timeout-s",
        type=float,
        default=DEFAULT_TIMEOUT_S,
        help=f"Per-request timeout in seconds (default: {DEFAULT_TIMEOUT_S}).",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Always exit 0. For monitoring that should observe, not gate.",
    )
    args = parser.parse_args(argv)

    results = [probe_one(url, timeout_s=args.timeout_s) for url in args.urls]
    print(format_report(results), flush=True)

    if args.report_only:
        return 0

    if any(not result.ok for result in results):
        sys.stderr.write(
            "\n".join(
                [
                    "",
                    "ERROR: /health/db reported an unhealthy database on at least one app.",
                    "",
                    "Incident #669 ran ~96 minutes with DB-backed routes 500ing while",
                    "/health stayed green, because nothing polled /health/db. Investigate",
                    "the database connection for the FAIL row(s) above.",
                    "",
                ]
            )
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
