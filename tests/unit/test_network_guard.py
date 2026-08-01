"""Runtime network-guard behavior for transactions and the writer lock."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import MagicMock

import httpx
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.config import settings
from app.services.embedding_service import embed_text
from app.services.summer_league.nba_stats_client import NBAStatsClient
from app.utils.network_guard import (
    NetworkIOGuardViolation,
    guard_network_io,
    guarded_async_httpx_event_hooks,
    guarded_httpx_event_hooks,
    mark_summer_league_writer_lock_acquired,
    transaction_depth,
    writer_lock_depth,
)


class _FakeTransaction:
    """Minimal active-transaction stand-in for writer-lock tracking."""

    def __init__(self) -> None:
        self.is_active = True


class _FakeSyncSession:
    """Expose the transaction API used by the writer-lock marker."""

    def __init__(self, transaction: _FakeTransaction) -> None:
        self._transaction = transaction

    def get_transaction(self) -> _FakeTransaction:
        """Return the active fake transaction."""
        return self._transaction


class _FakeAsyncSession:
    """Minimal AsyncSession shape needed by the writer-lock marker."""

    def __init__(self, transaction: _FakeTransaction) -> None:
        self.sync_session = _FakeSyncSession(transaction)


def _nested_network_call() -> None:
    """Intermediate helper proving the guard catches non-lexical nesting."""
    guard_network_io("nested unit-test request")


def test_httpx_hook_raises_through_nested_helper_in_open_transaction() -> None:
    """Dev/test blocks HTTPX at any call depth while a transaction is open."""
    engine = create_engine("sqlite://")
    request_reached_transport = False

    def _transport(request: httpx.Request) -> httpx.Response:
        nonlocal request_reached_transport
        request_reached_transport = True
        return httpx.Response(200, request=request)

    with Session(engine) as session:
        session.execute(text("SELECT 1"))
        assert transaction_depth() == 1
        with httpx.Client(
            transport=httpx.MockTransport(_transport),
            event_hooks=guarded_httpx_event_hooks(),
        ) as client:
            with pytest.raises(
                NetworkIOGuardViolation, match="database_transaction"
            ) as exc:
                client.get("https://example.test/guard")

    engine.dispose()
    assert request_reached_transport is False
    assert "test_httpx_hook_raises_through_nested_helper" in str(exc.value)
    assert transaction_depth() == 0


@pytest.mark.asyncio
async def test_async_httpx_hook_reaches_transport_without_transaction() -> None:
    """AsyncClient awaits its hook and reaches transport when no guard is active."""

    async def _transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_transport),
        event_hooks=guarded_async_httpx_event_hooks(),
    ) as client:
        response = await client.get("https://example.test/guard")

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_async_httpx_hook_blocks_open_transaction_before_transport() -> None:
    """AsyncClient's awaited hook blocks guarded I/O before its transport."""
    engine = create_engine("sqlite://")
    request_reached_transport = False

    async def _transport(request: httpx.Request) -> httpx.Response:
        nonlocal request_reached_transport
        request_reached_transport = True
        return httpx.Response(200, request=request)

    with Session(engine) as session:
        session.execute(text("SELECT 1"))
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(_transport),
            event_hooks=guarded_async_httpx_event_hooks(),
        ) as client:
            with pytest.raises(NetworkIOGuardViolation):
                await client.get("https://example.test/guard")

    engine.dispose()
    assert request_reached_transport is False


def test_transaction_guard_reports_intermediate_helper_stack() -> None:
    """A deeply nested guarded call includes its intermediate helper in the stack."""
    engine = create_engine("sqlite://")
    with Session(engine) as session:
        session.execute(text("SELECT 1"))
        with pytest.raises(NetworkIOGuardViolation) as exc:
            _nested_network_call()
    engine.dispose()

    assert "_nested_network_call" in str(exc.value)
    assert writer_lock_depth() == 0


def test_writer_lock_guard_is_independent_of_transaction_listener() -> None:
    """A tracked writer lock blocks network I/O without a listener-tracked transaction."""
    transaction = _FakeTransaction()
    db = _FakeAsyncSession(transaction)
    mark_summer_league_writer_lock_acquired(db)  # type: ignore[arg-type]

    assert transaction_depth() == 0
    assert writer_lock_depth() == 1
    with pytest.raises(NetworkIOGuardViolation, match="writer_lock_depth=1"):
        guard_network_io("writer-lock-only request")

    transaction.is_active = False
    assert writer_lock_depth() == 0


@pytest.mark.asyncio
async def test_child_task_inherits_frozen_transaction_depth_snapshot() -> None:
    """A task spawned mid-transaction freezes its own copy of the guard's state.

    ``ContextVar`` state is copied, not shared, at ``asyncio.create_task``
    time: a child task spawned while a transaction is open inherits that
    transaction as "active" in its own context, but never observes the
    parent's later ``after_transaction_end`` removal -- there is no
    cross-task propagation. This documents the resulting semantics: if the
    parent's transaction closes before the child checks
    :func:`transaction_depth`, the child reports a permanent false-positive
    depth for the rest of its life, even though the real transaction is long
    gone. No guarded production path uses ``create_task``/``gather`` today,
    so this is latent rather than an active bug -- this test exists so the
    behavior is explicit and intentional before the first concurrent
    refactor risks tripping over it silently.
    """
    engine = create_engine("sqlite://")
    child_ready = asyncio.Event()
    let_child_check = asyncio.Event()
    child_saw_depth: list[int] = []

    async def _child() -> None:
        child_ready.set()
        await let_child_check.wait()
        child_saw_depth.append(transaction_depth())

    with Session(engine) as session:
        session.execute(text("SELECT 1"))
        assert transaction_depth() == 1
        # copy_context() happens here, snapshotting the active transaction.
        task = asyncio.create_task(_child())
        await child_ready.wait()

    # The parent's context observes the transaction end (Session.__exit__
    # rolls back and fires after_transaction_end).
    assert transaction_depth() == 0

    let_child_check.set()
    await task
    engine.dispose()

    # The child's frozen context copy never received the removal: it still
    # believes the closed transaction is active.
    assert child_saw_depth == [1]


def test_production_warns_for_transaction_and_writer_lock(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Production logs both guard types with stacks without raising."""
    monkeypatch.setattr(settings, "env", "prod")
    caplog.set_level(logging.WARNING, logger="app.utils.network_guard")
    engine = create_engine("sqlite://")

    with Session(engine) as session:
        session.execute(text("SELECT 1"))
        guard_network_io("production transaction request")

    transaction = _FakeTransaction()
    mark_summer_league_writer_lock_acquired(  # type: ignore[arg-type]
        _FakeAsyncSession(transaction)
    )
    guard_network_io("production writer-lock request")
    transaction.is_active = False
    engine.dispose()

    assert "database_transaction_depth=1" in caplog.text
    assert "summer_league_writer_lock_depth=1" in caplog.text
    assert "Call stack:" in caplog.text


@pytest.mark.asyncio
async def test_embedding_client_checks_guard_before_api_call() -> None:
    """Gemini embedding refuses network I/O before invoking its client."""
    engine = create_engine("sqlite://")
    client = MagicMock()

    with Session(engine) as session:
        session.execute(text("SELECT 1"))
        with pytest.raises(NetworkIOGuardViolation, match="Gemini embedding"):
            await embed_text("Cooper Flagg", client=client)

    engine.dispose()
    client.aio.models.embed_content.assert_not_called()


def test_nba_stats_client_checks_guard_before_owned_session_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The production NBA transport enforces the transaction guard."""
    engine = create_engine("sqlite://")
    session_client = MagicMock()
    monkeypatch.setattr(
        "app.services.summer_league.nba_stats_client.cffi_requests.Session",
        lambda **_kwargs: session_client,
    )
    client = NBAStatsClient()

    with Session(engine) as session:
        session.execute(text("SELECT 1"))
        with pytest.raises(NetworkIOGuardViolation, match="NBA Stats request"):
            client.fetch_json("leaguegamelog", {"LeagueID": "15"})

    engine.dispose()
    session_client.get.assert_not_called()
