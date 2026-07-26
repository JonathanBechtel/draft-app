"""Unit tests for the database-exercising readiness probe.

Failure this descends from
--------------------------
Incident #669: DB-backed public routes returned 500 for roughly 96 minutes while
``/health`` kept returning 200, because the liveness endpoint issues no query. The
readiness probe added alongside it exists to go red in exactly that situation, so these
tests assert the behaviors that make it a usable operational signal:

* it reports 503 — not 200, and not an unhandled exception — when the database is
  unreachable;
* it is *bounded*, so a saturated pool surfaces as a fast failure rather than inheriting
  the 30-second ``pool_timeout`` that made the incident's routes hang;
* it never raises, because a probe that 500s tells a monitor less than one that reports;
* the liveness endpoint stays database-free, so a database outage cannot cause an
  orchestrator to cycle every web machine.

``AsyncEngine.connect`` and ``.sync_engine`` are read-only attributes, so these tests
swap the whole ``db_async.engine`` global for a stub. Both the probe and ``pool_stats()``
resolve that global at call time, which is what makes the substitution work.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

import pytest
from fastapi.testclient import TestClient

from app.utils import db_async


class _StubPool:
    """Minimal QueuePool stand-in exposing the gauges the probe reports."""

    def __init__(self, *, size: int = 5, checkedout: int = 5, overflow: int = 10):
        self._size = size
        self._checkedout = checkedout
        self._overflow = overflow

    def size(self) -> int:
        return self._size

    def checkedin(self) -> int:
        return self._size - self._checkedout

    def checkedout(self) -> int:
        return self._checkedout

    def overflow(self) -> int:
        return self._overflow


class _StubSyncEngine:
    def __init__(self, pool: Any):
        self.pool = pool


class _StubEngine:
    """Stands in for ``AsyncEngine``: a ``connect()`` factory plus a pool to inspect."""

    def __init__(self, connect_factory: Callable[[], Any], pool: Any = None):
        self._connect_factory = connect_factory
        self.sync_engine = _StubSyncEngine(pool if pool is not None else _StubPool())

    def connect(self) -> Any:
        return self._connect_factory()


class _OkConnection:
    """Async context manager whose ``execute`` succeeds immediately."""

    async def execute(self, _stmt: Any) -> None:
        return None

    async def __aenter__(self) -> "_OkConnection":
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        return False


class _HangingConnection:
    """Never becomes usable — models a pool with no free slots."""

    async def __aenter__(self) -> "_HangingConnection":
        await asyncio.sleep(60)
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        return False


def _install_engine(
    monkeypatch: pytest.MonkeyPatch,
    connect_factory: Callable[[], Any],
    pool: Any = None,
) -> None:
    """Replace the module-level engine with a stub for the duration of one test."""
    monkeypatch.setattr(db_async, "engine", _StubEngine(connect_factory, pool))


@pytest.mark.asyncio
async def test_probe_reports_ok_and_latency_when_database_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reachable database yields ``database_ok`` with a measured latency."""
    _install_engine(monkeypatch, _OkConnection)

    report = await db_async.check_database_readiness()

    assert report.database_ok is True
    assert report.error is None
    assert report.latency_ms is not None and report.latency_ms >= 0
    assert report.pool["checkedout"] == 5


@pytest.mark.asyncio
async def test_probe_reports_failure_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connection error is reported, not propagated.

    A probe that raises turns into a 500 and tells a monitor only "something broke";
    the point of this endpoint is to say *what* broke.
    """

    def _refused() -> Any:
        raise OSError("connection refused")

    _install_engine(monkeypatch, _refused)

    report = await db_async.check_database_readiness()

    assert report.database_ok is False
    assert report.latency_ms is None
    assert "connection refused" in (report.error or "")
    # Pool gauges are captured before the probe, so they survive the failure — this is
    # the saturation evidence the incident lacked.
    assert report.pool["overflow"] == 10


@pytest.mark.asyncio
async def test_probe_is_bounded_when_pool_is_saturated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hanging connection attempt times out well short of ``pool_timeout``.

    This is the incident's shape: requests waited the full 30-second pool timeout
    before failing. The probe must not inherit that wait.
    """
    _install_engine(monkeypatch, _HangingConnection)

    loop = asyncio.get_running_loop()
    started = loop.time()
    report = await db_async.check_database_readiness(timeout_seconds=0.05)
    elapsed = loop.time() - started

    assert report.database_ok is False
    assert "saturated" in (report.error or "")
    assert elapsed < 5, "probe must fail fast rather than wait on the pool"


def test_probe_timeout_default_is_below_pool_timeout() -> None:
    """The default bound stays under SQLAlchemy's 30s ``pool_timeout``.

    Guards the reason the constant exists: raising it past the pool timeout would
    silently restore the 30-second hang the probe was written to avoid.
    """
    assert 0 < db_async.READINESS_TIMEOUT_SECONDS < 30


def test_pool_stats_tolerates_pools_without_queue_gauges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``NullPool``-style pools report what they have instead of erroring.

    Test and script configurations do not all use ``QueuePool``; the probe must not
    become the thing that breaks in those environments.
    """

    class _NullPool:
        def size(self) -> int:
            return 0

    _install_engine(monkeypatch, _OkConnection, pool=_NullPool())

    assert db_async.pool_stats() == {"size": 0}


def test_liveness_endpoint_issues_no_database_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/health`` stays green — and query-free — even with the database down.

    Liveness must not depend on the database: an orchestrator restarting every web
    machine during a database outage makes the outage worse.
    """
    from app.main import app

    def _fail_loudly() -> Any:
        raise AssertionError("liveness probe must not touch the database")

    _install_engine(monkeypatch, _fail_loudly)

    # No context manager: lifespan (and its optional init_db) must not run for a
    # DB-free unit test.
    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_endpoint_returns_503_when_database_is_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/health/db`` reports 503 with diagnostic detail when the database fails."""
    from app.main import app

    def _refused() -> Any:
        raise OSError("connection refused")

    _install_engine(monkeypatch, _refused)

    response = TestClient(app).get("/health/db")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["database_ok"] is False
    assert "connection refused" in body["error"]
    assert body["pool"]["checkedout"] == 5


def test_readiness_endpoint_returns_200_when_database_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/health/db`` reports 200 and the pool gauges on the healthy path."""
    from app.main import app

    _install_engine(monkeypatch, _OkConnection)

    response = TestClient(app).get("/health/db")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database_ok"] is True
    assert "error" not in body
    assert body["pool"]["size"] == 5
