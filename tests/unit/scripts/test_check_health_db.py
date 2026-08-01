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

The HTTP layer is stubbed at the lowest possible seam. Direct ``probe_one`` tests use
the ``fetch`` injection point; the ``main()`` tests instead stub ``urlopen`` itself, so
argv -> ``argparse`` -> the real ``probe_one`` -> the real ``_default_fetch`` ->
status classification all run for real. Stubbing ``probe_one`` there would mock the
very composition those tests exist to verify. Nothing here touches a real network.
"""

from __future__ import annotations

import io
import json
from urllib.error import HTTPError, URLError

import pytest

from scripts.check_health_db import _default_fetch, main, probe_one


def _fetch_returning(status: int, body: bytes = b"{}"):
    def _fetch(url: str, timeout_s: float) -> tuple[int, bytes]:
        return status, body

    return _fetch


def _fetch_raising(exc: Exception):
    def _fetch(url: str, timeout_s: float):
        raise exc

    return _fetch


class _FakeResponse:
    """Minimal ``urlopen`` return value: a context manager with status + body."""

    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _stub_urlopen(monkeypatch: pytest.MonkeyPatch, status_by_url):
    """Replace ``urlopen`` in the script's namespace and record every call.

    Args:
        monkeypatch: Active pytest monkeypatch fixture.
        status_by_url: Maps the probed URL to the ``(status, body)`` (or an
            exception to raise) the fake response should produce.

    Returns:
        The list that each ``(url, timeout)`` call is appended to, so tests can
        assert the URL construction and ``--timeout-s`` threading that
        ``main()`` performed for real.
    """
    calls: list[tuple[str, float]] = []

    def _fake_urlopen(url: str, timeout: float = 0.0):
        calls.append((url, timeout))
        outcome = status_by_url[url]
        if isinstance(outcome, Exception):
            raise outcome
        status, body = outcome
        return _FakeResponse(status, body)

    monkeypatch.setattr("scripts.check_health_db.urlopen", _fake_urlopen)
    return calls


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


def test_a_non_json_error_body_still_reports_the_status() -> None:
    """A malformed or empty error body must not crash the probe."""
    result = probe_one(
        "https://draft-app-prod.fly.dev", fetch=_fetch_returning(503, b"not json")
    )

    assert result.ok is False
    assert result.status_code == 503
    assert "HTTP 503" in result.detail


def test_default_fetch_reads_an_http_error_as_a_response_not_an_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 503 must come back as ``(503, body)``, since urlopen raises on non-2xx.

    This is the only nontrivial branch in the real HTTP layer and the sole
    reason an unhealthy app reads as data rather than crashing the monitor.
    """
    _stub_urlopen(
        monkeypatch,
        {
            "https://draft-app-prod.fly.dev/health/db": HTTPError(
                "https://draft-app-prod.fly.dev/health/db",
                503,
                "Service Unavailable",
                {},  # type: ignore[arg-type]
                io.BytesIO(json.dumps({"error": "pool exhausted"}).encode()),
            )
        },
    )

    status, body = _default_fetch("https://draft-app-prod.fly.dev/health/db", 15.0)

    assert status == 503
    assert b"pool exhausted" in body


def test_main_fails_when_any_probed_app_is_unhealthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One bad app among several must still fail the run.

    Runs the whole argv -> probe -> classify -> exit-code composition for
    real; only ``urlopen`` is stubbed.
    """
    calls = _stub_urlopen(
        monkeypatch,
        {
            "https://draft-app.fly.dev/health/db": (200, b"{}"),
            "https://draft-app-prod.fly.dev/health/db": (
                503,
                json.dumps({"error": "pool exhausted"}).encode(),
            ),
        },
    )

    exit_code = main(
        [
            "--url",
            "https://draft-app.fly.dev",
            "--url",
            "https://draft-app-prod.fly.dev",
        ]
    )

    assert exit_code == 1
    assert [url for url, _ in calls] == [
        "https://draft-app.fly.dev/health/db",
        "https://draft-app-prod.fly.dev/health/db",
    ]


def test_main_succeeds_when_every_probed_app_is_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The all-clear case, end to end through the real probe."""
    _stub_urlopen(monkeypatch, {"https://draft-app.fly.dev/health/db": (200, b"{}")})

    exit_code = main(["--url", "https://draft-app.fly.dev"])

    assert exit_code == 0


def test_main_threads_the_timeout_flag_through_to_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--timeout-s`` must reach the actual HTTP call, parsed as a float.

    A mis-declared ``type=`` on the flag, or a value never threaded through
    ``probe_one``, would leave the probe on the default timeout with a fully
    green suite.
    """
    calls = _stub_urlopen(
        monkeypatch, {"https://draft-app.fly.dev/health/db": (200, b"{}")}
    )

    exit_code = main(["--url", "https://draft-app.fly.dev", "--timeout-s", "2.5"])

    assert exit_code == 0
    assert calls == [("https://draft-app.fly.dev/health/db", 2.5)]


def test_main_defaults_to_the_module_timeout_when_the_flag_is_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Omitting ``--timeout-s`` uses ``DEFAULT_TIMEOUT_S``, not an unbounded wait."""
    from scripts.check_health_db import DEFAULT_TIMEOUT_S

    calls = _stub_urlopen(
        monkeypatch, {"https://draft-app.fly.dev/health/db": (200, b"{}")}
    )

    main(["--url", "https://draft-app.fly.dev"])

    assert calls == [("https://draft-app.fly.dev/health/db", DEFAULT_TIMEOUT_S)]


def test_main_reports_an_unreachable_app_as_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transport-level failure reaching main() must exit 1, not raise."""
    _stub_urlopen(
        monkeypatch,
        {
            "https://draft-app-prod.fly.dev/health/db": URLError(
                "Name or service not known"
            )
        },
    )

    exit_code = main(["--url", "https://draft-app-prod.fly.dev"])

    assert exit_code == 1


def test_report_only_always_exits_zero_even_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--report-only is the escape hatch for observing without gating."""
    _stub_urlopen(
        monkeypatch,
        {"https://draft-app-prod.fly.dev/health/db": (503, b"{}")},
    )

    exit_code = main(["--url", "https://draft-app-prod.fly.dev", "--report-only"])

    assert exit_code == 0
