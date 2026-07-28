"""Runtime guard against network I/O inside database critical sections."""

from __future__ import annotations

import logging
import traceback
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from sqlalchemy import event
from sqlalchemy.orm import Session, SessionTransaction

if TYPE_CHECKING:
    from httpx import Request
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

_active_transaction_ids: ContextVar[frozenset[int]] = ContextVar(
    "active_database_transaction_ids",
    default=frozenset(),
)
_writer_lock_transactions: ContextVar[tuple[SessionTransaction, ...]] = ContextVar(
    "summer_league_writer_lock_transactions",
    default=(),
)


class NetworkIOGuardViolation(RuntimeError):
    """Raised outside production when network I/O starts in a critical section."""


@event.listens_for(Session, "after_begin")
def _track_transaction_begin(
    session: Session,
    transaction: SessionTransaction,
    connection: Any,
) -> None:
    """Add a physically-open SQLAlchemy transaction to this execution context."""
    del session, connection
    active = _active_transaction_ids.get()
    _active_transaction_ids.set(active | {id(transaction)})


@event.listens_for(Session, "after_transaction_end")
def _track_transaction_end(
    session: Session,
    transaction: SessionTransaction,
) -> None:
    """Remove a completed SQLAlchemy transaction from this execution context."""
    del session
    active = _active_transaction_ids.get()
    transaction_id = id(transaction)
    if transaction_id in active:
        _active_transaction_ids.set(active - {transaction_id})


def transaction_depth() -> int:
    """Return the number of physically-open transactions in this context."""
    return len(_active_transaction_ids.get())


def mark_summer_league_writer_lock_acquired(db: AsyncSession) -> None:
    """Track the active transaction that acquired the Summer League writer lock."""
    transaction = db.sync_session.get_transaction()
    if transaction is None or not transaction.is_active:
        raise RuntimeError(
            "Summer League writer lock acquired without an active transaction."
        )
    tracked = tuple(item for item in _writer_lock_transactions.get() if item.is_active)
    if all(item is not transaction for item in tracked):
        tracked += (transaction,)
    _writer_lock_transactions.set(tracked)


def writer_lock_depth() -> int:
    """Return the number of active writer-lock-owning transactions in this context."""
    tracked = tuple(
        transaction
        for transaction in _writer_lock_transactions.get()
        if transaction.is_active
    )
    if tracked != _writer_lock_transactions.get():
        _writer_lock_transactions.set(tracked)
    return len(tracked)


def guard_network_io(operation: str) -> None:
    """Raise or warn when network I/O starts in a guarded critical section."""
    transaction_count = transaction_depth()
    writer_lock_count = writer_lock_depth()
    if transaction_count == 0 and writer_lock_count == 0:
        return

    reasons: list[str] = []
    if transaction_count:
        reasons.append(f"database_transaction_depth={transaction_count}")
    if writer_lock_count:
        reasons.append(f"summer_league_writer_lock_depth={writer_lock_count}")
    stack = "".join(traceback.format_stack())
    message = (
        f"Network I/O guard blocked {operation}; {' '.join(reasons)}.\n"
        f"Call stack:\n{stack}"
    )
    from app.config import settings  # noqa: PLC0415

    if settings.env == "prod":
        logger.warning(message)
        return
    raise NetworkIOGuardViolation(message)


def guard_httpx_request(request: Request) -> None:
    """HTTPX request event hook that enforces the critical-section guard."""
    guard_network_io(f"HTTPX {request.method} {request.url}")


async def guard_async_httpx_request(request: Request) -> None:
    """AsyncClient-compatible request hook enforcing the same guard."""
    guard_httpx_request(request)


def guarded_httpx_event_hooks() -> dict[str, list[Any]]:
    """Return fresh synchronous HTTPX event-hook lists."""
    return {"request": [guard_httpx_request]}


def guarded_async_httpx_event_hooks() -> dict[str, list[Any]]:
    """Return fresh asynchronous HTTPX event-hook lists."""
    return {"request": [guard_async_httpx_request]}
