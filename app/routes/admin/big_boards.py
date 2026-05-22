"""Admin routes for manual big-board entry, review, and approval.

Workflow:
    1. Admin creates an empty PENDING board (source + draft year + published_at).
    2. Admin adds entries one at a time on the detail page (autocomplete to
       the existing /players/search endpoint).
    3. Admin approves or rejects. APPROVED and REJECTED boards are
       immutable; only PENDING boards may be edited or deleted.
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app.routes.admin.helpers import (
    base_context_with_permissions,
    require_dataset_access,
)
from app.schemas.big_boards import BoardStatus
from app.schemas.news_sources import NewsSource
from app.schemas.players_master import PlayerMaster
from app.services import big_board_service as svc
from app.utils.db_async import get_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/big-boards", tags=["admin-big-boards"])

_SUCCESS_MESSAGES: dict[str, str] = {
    "created": "Big board created. Add entries below.",
    "entry_added": "Entry added.",
    "entry_updated": "Entry updated.",
    "entry_deleted": "Entry removed.",
    "approved": "Board approved.",
    "rejected": "Board rejected.",
    "deleted": "Board deleted.",
}


@router.get("", response_class=HTMLResponse)
async def list_big_boards(
    request: Request,
    status: str | None = Query(default=None),
    success: str | None = Query(default=None),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """List big boards, optionally filtered by status."""
    redirect, user = await require_dataset_access(
        request, db, "big_boards", need_edit=False, next_path="/admin/big-boards"
    )
    if redirect:
        return redirect
    assert user is not None

    status_filter: BoardStatus | None = None
    if status:
        try:
            status_filter = BoardStatus(status.upper())
        except ValueError:
            status_filter = None

    boards = await svc.list_boards(db, status=status_filter)

    source_ids = {b.news_source_id for b in boards}
    sources_by_id: dict[int, NewsSource] = {}
    if source_ids:
        rows = await db.execute(
            select(NewsSource).where(NewsSource.id.in_(source_ids))  # type: ignore[union-attr]
        )
        sources_by_id = {s.id: s for s in rows.scalars().all() if s.id is not None}

    return request.app.state.templates.TemplateResponse(
        "admin/big-boards/index.html",
        await base_context_with_permissions(
            request,
            db,
            user,
            boards=boards,
            sources_by_id=sources_by_id,
            status_filter=status_filter.value if status_filter else None,
            statuses=[s.value for s in BoardStatus],
            success=_SUCCESS_MESSAGES.get(success) if success else None,
        ),
    )


@router.get("/new", response_class=HTMLResponse)
async def new_big_board(
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Display the create-board form."""
    redirect, user = await require_dataset_access(
        request,
        db,
        "big_boards",
        need_edit=True,
        next_path="/admin/big-boards/new",
    )
    if redirect:
        return redirect
    assert user is not None

    sources = (
        (
            await db.execute(
                select(NewsSource)
                .where(NewsSource.is_active)  # type: ignore[arg-type]
                .order_by(NewsSource.display_name)
            )
        )
        .scalars()
        .all()
    )

    return request.app.state.templates.TemplateResponse(
        "admin/big-boards/new.html",
        await base_context_with_permissions(
            request,
            db,
            user,
            sources=sources,
            default_draft_year=datetime.utcnow().year + 1,
            error=None,
        ),
    )


@router.post("", response_class=HTMLResponse)
async def create_big_board(
    request: Request,
    news_source_id: int = Form(...),
    draft_year: int = Form(...),
    published_at: str = Form(...),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Create an empty PENDING board and redirect to its detail page."""
    redirect, user = await require_dataset_access(
        request, db, "big_boards", need_edit=True, next_path="/admin/big-boards"
    )
    if redirect:
        return redirect
    assert user is not None

    try:
        published_dt = datetime.fromisoformat(published_at)
    except ValueError:
        return await _render_new_with_error(
            request,
            db,
            user,
            "Published-at must be an ISO date/time (e.g., 2026-05-20).",
        )
    if published_dt.tzinfo is not None:
        published_dt = published_dt.replace(tzinfo=None)

    async with db.begin():
        board = await svc.create_board(
            db,
            news_source_id=news_source_id,
            draft_year=draft_year,
            published_at=published_dt,
            entries=[],
        )

    return RedirectResponse(
        url=f"/admin/big-boards/{board.id}?success=created", status_code=303
    )


@router.get("/{board_id}", response_class=HTMLResponse)
async def big_board_detail(
    request: Request,
    board_id: int,
    success: str | None = Query(default=None),
    error: str | None = Query(default=None),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Show a single board, its entries, and the add/approve/reject controls."""
    redirect, user = await require_dataset_access(
        request,
        db,
        "big_boards",
        need_edit=False,
        next_path=f"/admin/big-boards/{board_id}",
    )
    if redirect:
        return redirect
    assert user is not None

    try:
        board, entries = await svc.get_board_with_entries(db, board_id)
    except svc.BoardNotFoundError:
        return RedirectResponse(url="/admin/big-boards", status_code=303)

    source = await db.get(NewsSource, board.news_source_id)

    player_ids = [e.player_id for e in entries]
    players_by_id: dict[int, PlayerMaster] = {}
    if player_ids:
        rows = await db.execute(
            select(PlayerMaster).where(PlayerMaster.id.in_(player_ids))  # type: ignore[union-attr]
        )
        players_by_id = {p.id: p for p in rows.scalars().all() if p.id is not None}

    return request.app.state.templates.TemplateResponse(
        "admin/big-boards/detail.html",
        await base_context_with_permissions(
            request,
            db,
            user,
            board=board,
            entries=entries,
            source=source,
            players_by_id=players_by_id,
            is_pending=board.status is BoardStatus.PENDING,
            success=_SUCCESS_MESSAGES.get(success) if success else None,
            error=error,
        ),
    )


@router.post("/{board_id}/entries", response_class=HTMLResponse)
async def add_board_entry(
    request: Request,
    board_id: int,
    player_id: int = Form(...),
    rank: int = Form(...),
    tier: str | None = Form(default=None),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Append an entry to a PENDING board."""
    redirect, user = await require_dataset_access(
        request,
        db,
        "big_boards",
        need_edit=True,
        next_path=f"/admin/big-boards/{board_id}",
    )
    if redirect:
        return redirect
    assert user is not None

    tier_int = _parse_optional_int(tier)
    try:
        async with db.begin():
            await svc.add_entry(
                db,
                board_id=board_id,
                player_id=player_id,
                rank=rank,
                tier=tier_int,
            )
    except svc.BigBoardError as exc:
        return _redirect_with_error(board_id, str(exc))

    return RedirectResponse(
        url=f"/admin/big-boards/{board_id}?success=entry_added", status_code=303
    )


@router.post("/{board_id}/entries/{entry_id}/update", response_class=HTMLResponse)
async def update_board_entry(
    request: Request,
    board_id: int,
    entry_id: int,
    rank: int = Form(...),
    tier: str | None = Form(default=None),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Edit rank/tier on a PENDING board's entry."""
    redirect, user = await require_dataset_access(
        request,
        db,
        "big_boards",
        need_edit=True,
        next_path=f"/admin/big-boards/{board_id}",
    )
    if redirect:
        return redirect
    assert user is not None

    tier_int = _parse_optional_int(tier)
    try:
        async with db.begin():
            await svc.update_entry(db, entry_id=entry_id, rank=rank, tier=tier_int)
    except svc.BigBoardError as exc:
        return _redirect_with_error(board_id, str(exc))

    return RedirectResponse(
        url=f"/admin/big-boards/{board_id}?success=entry_updated", status_code=303
    )


@router.post("/{board_id}/entries/{entry_id}/delete", response_class=HTMLResponse)
async def delete_board_entry(
    request: Request,
    board_id: int,
    entry_id: int,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Remove an entry from a PENDING board."""
    redirect, user = await require_dataset_access(
        request,
        db,
        "big_boards",
        need_edit=True,
        next_path=f"/admin/big-boards/{board_id}",
    )
    if redirect:
        return redirect
    assert user is not None

    try:
        async with db.begin():
            await svc.delete_entry(db, entry_id=entry_id)
    except svc.BigBoardError as exc:
        return _redirect_with_error(board_id, str(exc))

    return RedirectResponse(
        url=f"/admin/big-boards/{board_id}?success=entry_deleted", status_code=303
    )


@router.post("/{board_id}/approve", response_class=HTMLResponse)
async def approve_big_board(
    request: Request,
    board_id: int,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Approve a PENDING board, locking it for downstream consensus."""
    redirect, user = await require_dataset_access(
        request,
        db,
        "big_boards",
        need_edit=True,
        next_path=f"/admin/big-boards/{board_id}",
    )
    if redirect:
        return redirect
    assert user is not None

    try:
        async with db.begin():
            await svc.approve_board(db, board_id=board_id)
    except svc.BigBoardError as exc:
        return _redirect_with_error(board_id, str(exc))

    return RedirectResponse(
        url=f"/admin/big-boards/{board_id}?success=approved", status_code=303
    )


@router.post("/{board_id}/reject", response_class=HTMLResponse)
async def reject_big_board(
    request: Request,
    board_id: int,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Reject a PENDING board (kept for audit, not used for consensus)."""
    redirect, user = await require_dataset_access(
        request,
        db,
        "big_boards",
        need_edit=True,
        next_path=f"/admin/big-boards/{board_id}",
    )
    if redirect:
        return redirect
    assert user is not None

    try:
        async with db.begin():
            await svc.reject_board(db, board_id=board_id)
    except svc.BigBoardError as exc:
        return _redirect_with_error(board_id, str(exc))

    return RedirectResponse(
        url=f"/admin/big-boards/{board_id}?success=rejected", status_code=303
    )


@router.post("/{board_id}/delete", response_class=HTMLResponse)
async def delete_big_board(
    request: Request,
    board_id: int,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Hard-delete a PENDING board (for typos; use reject for audit trail)."""
    redirect, user = await require_dataset_access(
        request,
        db,
        "big_boards",
        need_edit=True,
        next_path=f"/admin/big-boards/{board_id}",
    )
    if redirect:
        return redirect
    assert user is not None

    try:
        async with db.begin():
            await svc.delete_board(db, board_id=board_id)
    except svc.BigBoardError as exc:
        return _redirect_with_error(board_id, str(exc))

    return RedirectResponse(url="/admin/big-boards?success=deleted", status_code=303)


def _parse_optional_int(raw: str | None) -> int | None:
    if raw is None:
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


def _redirect_with_error(board_id: int, message: str) -> Response:
    from urllib.parse import quote

    return RedirectResponse(
        url=f"/admin/big-boards/{board_id}?error={quote(message)}",
        status_code=303,
    )


async def _render_new_with_error(
    request: Request,
    db: AsyncSession,
    user: object,
    message: str,
) -> Response:
    sources = (
        (
            await db.execute(
                select(NewsSource)
                .where(NewsSource.is_active)  # type: ignore[arg-type]
                .order_by(NewsSource.display_name)
            )
        )
        .scalars()
        .all()
    )
    return request.app.state.templates.TemplateResponse(
        "admin/big-boards/new.html",
        await base_context_with_permissions(
            request,
            db,
            user,  # type: ignore[arg-type]
            sources=sources,
            default_draft_year=datetime.utcnow().year + 1,
            error=message,
        ),
    )
