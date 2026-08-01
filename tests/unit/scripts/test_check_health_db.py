"""Unit tests for the ``/health/db`` probe script.

Failure this closes
--------------------
PR #684 shipped ``/health/db`` specifically so incident #669's shape (public routes
500ing while a DB-free ``/health`` stayed green) would be visible, but nothing in the
repo polled it -- wiring a monitor was left as an operator step outside this codebase.
These tests pin the behaviors that make the probe a signal:

* a 200 response is healthy, any other status is not;
* a probed app being fully unreachable degrades to a reported failure, not a crash;
* multiple apps are reported independently, and one failing app still surfaces
  another's result;
* ``--report-only`` always exits 0, for a caller that wants to observe without gating.

The HTTP layer is stubbed via the ``fetch`` injection point; nothing here touches a
real network.
"""

from __future__ import annotations

import json
from urllib.error import URLError

import pytest

from scripts.check_health_db import HealthProbeResult, main, probe_one


def _fetch_returning(status: int, body: bytes = b"{}"):
    def _fetch(url: str, timeout_s: float) -> tuple[int, bytes]:
        return status, body

    return _fetch


def _fetch_raising(exc: Exception):
    def _fetch(url: str, timeout_s: float):
        raise exc

    return _fetch


def test_a_200_response_is_healthy() -> None:
    """The happy path: the app can reach its database."""
    result = probe_one("https://draft-app-prod.fly.dev", fetch=_fetch_returning(200))

    assert result.ok is True
    assert result.status_code == 200
    assert result.url == "https://draft-app-prod.fly.dev/health/db"


def test_trailing_slash_on_the_app_url_does_not_double_up() -> None:
    """A base URL with a trailing slash must still probe the right path."""
    result = probe_one("https://draft-app.fly.dev/", fetch=_fetch_returning(200))

    assert result.url == "https://draft-app.fly.dev/health/db"


def test_a_503_is_reported_as_unhealthy_with_the_error_surfaced() -> None:
    """The shape /health/db actually returns on failure (503 + an error field)."""
    body = json.dumps({"status": "unavailable", "error": "pool exhausted"}).encode()
    result = probe_one(
        "https://draft-app-prod.fly.dev", fetch=_fetch_returning(503, body)
    )

    assert result.ok is False
    assert result.status_code == 503
    assert "pool exhausted" in result.detail


def test_an_unreachable_app_degrades_to_a_reported_failure_not_a_crash() -> None:
    """DNS failure / connection refused must not raise out of the monitor.

    A probe that crashes produces the same silence #669's shape did -- nothing
    reports it.
    """
    result = probe_one(
        "https://draft-app-prod.fly.dev",
        fetch=_fetch_raising(URLError("Name or service not known")),
    )

    assert result.ok is False
    assert result.status_code is None
    assert "unreachable" in result.detail


def test_an_unexpected_exception_also_degrades_rather_than_propagating() -> None:
    """Any error shape from the fetch layer must land as a reported result."""
    result = probe_one(
        "https://draft-app-prod.fly.dev",
        fetch=_fetch_raising(TimeoutError("timed out")),
    )

    assert result.ok is False
    assert result.status_code is None


def test_a_non_json_error_body_still_reports_the_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed or empty error body must not crash the probe."""
    result = probe_one(
        "https://draft-app-prod.fly.dev", fetch=_fetch_returning(503, b"not json")
    )

    assert result.ok is False
    assert result.status_code == 503
    assert "HTTP 503" in result.detail


def test_main_fails_when_any_probed_app_is_unhealthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One bad app among several must still fail the run."""

    def fake_probe(app_url: str, *, fetch=None, timeout_s=15.0) -> HealthProbeResult:
        ok = app_url != "https://draft-app-prod.fly.dev"
        return HealthProbeResult(
            url=app_url + "/health/db",
            ok=ok,
            status_code=200 if ok else 503,
            detail="ok" if ok else "HTTP 503",
        )

    monkeypatch.setattr("scripts.check_health_db.probe_one", fake_probe)

    exit_code = main(
        [
            "--url",
            "https://draft-app.fly.dev",
            "--url",
            "https://draft-app-prod.fly.dev",
        ]
    )

    assert exit_code == 1


def test_main_succeeds_when_every_probed_app_is_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The all-clear case."""

    def fake_probe(app_url: str, *, fetch=None, timeout_s=15.0) -> HealthProbeResult:
        return HealthProbeResult(
            url=app_url + "/health/db", ok=True, status_code=200, detail="ok"
        )

    monkeypatch.setattr("scripts.check_health_db.probe_one", fake_probe)

    exit_code = main(["--url", "https://draft-app.fly.dev"])

    assert exit_code == 0


def test_report_only_always_exits_zero_even_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--report-only is the escape hatch for observing without gating."""

    def fake_probe(app_url: str, *, fetch=None, timeout_s=15.0) -> HealthProbeResult:
        return HealthProbeResult(
            url=app_url + "/health/db", ok=False, status_code=503, detail="HTTP 503"
        )

    monkeypatch.setattr("scripts.check_health_db.probe_one", fake_probe)

    exit_code = main(["--url", "https://draft-app-prod.fly.dev", "--report-only"])

    assert exit_code == 0
