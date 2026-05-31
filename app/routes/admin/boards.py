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
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app.routes.admin.helpers import (
    base_context_with_permissions,
    require_dataset_access,
)
from app.schemas.boards import BoardKind, BoardStatus, ResolutionMethod
from app.schemas.news_items import NewsItem
from app.schemas.news_sources import NewsSource
from app.schemas.nba_teams import NbaTeam
from app.schemas.players_master import PlayerMaster
from app.services import board_service as svc
from app.services.player_search_service import find_lexical_players
from app.utils.db_async import get_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/boards", tags=["admin-boards"])

_SUCCESS_MESSAGES: dict[str, str] = {
    "created": "Big board created. Add entries below.",
    "entry_added": "Entry added.",
    "entry_updated": "Entry updated.",
    "entry_deleted": "Entry removed.",
    "stub_minted": "Stub player created and entry resolved.",
    "approved": "Board approved.",
    "rejected": "Board rejected.",
    "deleted": "Board deleted.",
    "reopened": "Board reopened for editing.",
    "cloned": "Board cloned. Edit the new copy below.",
    "meta_updated": "Board details updated.",
    "extracted": "Board extracted successfully. Review and resolve unresolved entries below.",
    "already_extracted": "A board for this article already existed — shown below.",
}


@router.get("", response_class=HTMLResponse)
async def list_big_boards(
    request: Request,
    status: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    success: str | None = Query(default=None),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """List boards, optionally filtered by status and/or kind."""
    redirect, user = await require_dataset_access(
        request, db, "boards", need_edit=False, next_path="/admin/boards"
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

    kind_filter: BoardKind | None = None
    if kind:
        try:
            kind_filter = BoardKind(kind.upper())
        except ValueError:
            kind_filter = None

    boards = await svc.list_boards(db, status=status_filter, kind=kind_filter)

    source_ids = {b.news_source_id for b in boards}
    sources_by_id: dict[int, NewsSource] = {}
    if source_ids:
        rows = await db.execute(
            select(NewsSource).where(NewsSource.id.in_(source_ids))  # type: ignore[union-attr]
        )
        sources_by_id = {s.id: s for s in rows.scalars().all() if s.id is not None}

    return request.app.state.templates.TemplateResponse(
        "admin/boards/index.html",
        await base_context_with_permissions(
            request,
            db,
            user,
            boards=boards,
            sources_by_id=sources_by_id,
            status_filter=status_filter.value if status_filter else None,
            kind_filter=kind_filter.value if kind_filter else None,
            statuses=[s.value for s in BoardStatus],
            kinds=[k.value for k in BoardKind],
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
        "boards",
        need_edit=True,
        next_path="/admin/boards/new",
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
        "admin/boards/new.html",
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
        request, db, "boards", need_edit=True, next_path="/admin/boards"
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
        url=f"/admin/boards/{board.id}?success=created", status_code=303
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
        "boards",
        need_edit=False,
        next_path=f"/admin/boards/{board_id}",
    )
    if redirect:
        return redirect
    assert user is not None

    try:
        board, entries = await svc.get_board_with_entries(db, board_id)
    except svc.BoardNotFoundError:
        return RedirectResponse(url="/admin/boards", status_code=303)

    source = await db.get(NewsSource, board.news_source_id)

    # Fetch source article for side-by-side display.
    news_item: NewsItem | None = None
    if board.news_item_id is not None:
        news_item = await db.get(NewsItem, board.news_item_id)

    player_ids = [e.player_id for e in entries if e.player_id is not None]
    players_by_id: dict[int, PlayerMaster] = {}
    if player_ids:
        rows = await db.execute(
            select(PlayerMaster).where(PlayerMaster.id.in_(player_ids))  # type: ignore[union-attr]
        )
        players_by_id = {p.id: p for p in rows.scalars().all() if p.id is not None}

    # Fetch NBA teams for mock-draft entry display.
    team_ids: set[int] = set()
    for e in entries:
        if e.team_id is not None:
            team_ids.add(e.team_id)
        if e.original_team_id is not None:
            team_ids.add(e.original_team_id)
    teams_by_id: dict[int, NbaTeam] = {}
    if team_ids:
        team_rows = await db.execute(
            select(NbaTeam).where(NbaTeam.id.in_(team_ids))  # type: ignore[union-attr]
        )
        teams_by_id = {t.id: t for t in team_rows.scalars().all() if t.id is not None}

    # All active teams for the mock-draft entry add/edit dropdowns.
    all_teams: list[NbaTeam] = []
    if board.kind is BoardKind.MOCK_DRAFT:
        all_teams = list(
            (await db.execute(select(NbaTeam).order_by(NbaTeam.name))).scalars().all()
        )

    unresolved_count = sum(
        1 for e in entries if e.resolution_method is ResolutionMethod.UNRESOLVED
    )

    is_pending = board.status is BoardStatus.PENDING
    default_tier = (
        await svc.latest_entry_tier(db, board_id=board_id) if is_pending else None
    )
    default_next_rank = (
        (max((e.position for e in entries), default=0) + 1) if entries else 1
    )

    editable_sources: list[NewsSource] = []
    if is_pending:
        active_rows = list(
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
        # Always include the board's current source in the dropdown, even
        # if it was deactivated after the board was created. Otherwise a
        # year/date-only edit would silently reassign attribution to
        # whichever active source the required <select> defaulted to.
        if source is not None and not any(s.id == source.id for s in active_rows):
            active_rows.append(source)
            active_rows.sort(key=lambda s: (s.display_name or "").lower())
        editable_sources = active_rows

    return request.app.state.templates.TemplateResponse(
        "admin/boards/detail.html",
        await base_context_with_permissions(
            request,
            db,
            user,
            board=board,
            entries=entries,
            source=source,
            news_item=news_item,
            players_by_id=players_by_id,
            teams_by_id=teams_by_id,
            all_teams=all_teams,
            unresolved_count=unresolved_count,
            is_pending=is_pending,
            default_tier=default_tier,
            default_next_rank=default_next_rank,
            editable_sources=editable_sources,
            success=_SUCCESS_MESSAGES.get(success) if success else None,
            error=error,
        ),
    )


@router.post("/{board_id}/entries", response_class=HTMLResponse)
async def add_board_entry(
    request: Request,
    board_id: int,
    player_id: int = Form(...),
    position: int = Form(...),
    tier: str | None = Form(default=None),
    round: str | None = Form(default=None),
    team_id: str | None = Form(default=None),
    original_team_id: str | None = Form(default=None),
    trade_note: str | None = Form(default=None),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Append an entry to a PENDING board.

    Accepts both BIG_BOARD fields (``tier``) and MOCK_DRAFT fields
    (``round``, ``team_id``, ``original_team_id``, ``trade_note``).
    Extra fields submitted for the wrong board kind are silently ignored
    by the service — the schema columns are nullable so there is no DB
    error.
    """
    redirect, user = await require_dataset_access(
        request,
        db,
        "boards",
        need_edit=True,
        next_path=f"/admin/boards/{board_id}",
    )
    if redirect:
        return redirect
    assert user is not None

    tier_int = _parse_optional_int(tier)
    round_int = _parse_optional_int(round)
    team_id_int = _parse_optional_int(team_id)
    orig_team_id_int = _parse_optional_int(original_team_id)
    trade_note_str = (trade_note or "").strip() or None
    try:
        async with db.begin():
            await svc.add_entry(
                db,
                board_id=board_id,
                player_id=player_id,
                position=position,
                tier=tier_int,
                round=round_int,
                team_id=team_id_int,
                original_team_id=orig_team_id_int,
                trade_note=trade_note_str,
            )
    except svc.BoardError as exc:
        return _redirect_with_error(board_id, str(exc))

    return RedirectResponse(
        url=f"/admin/boards/{board_id}?success=entry_added", status_code=303
    )


@router.post("/{board_id}/entries/{entry_id}/update", response_class=HTMLResponse)
async def update_board_entry(
    request: Request,
    board_id: int,
    entry_id: int,
    position: int = Form(...),
    tier: str | None = Form(default=None),
    round: str | None = Form(default=None),
    team_id: str | None = Form(default=None),
    original_team_id: str | None = Form(default=None),
    trade_note: str | None = Form(default=None),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Edit rank/tier (big board) or pick/team/round (mock draft) on a PENDING entry."""
    redirect, user = await require_dataset_access(
        request,
        db,
        "boards",
        need_edit=True,
        next_path=f"/admin/boards/{board_id}",
    )
    if redirect:
        return redirect
    assert user is not None

    tier_int = _parse_optional_int(tier)
    round_int = _parse_optional_int(round)
    team_id_int = _parse_optional_int(team_id)
    orig_team_id_int = _parse_optional_int(original_team_id)
    trade_note_str = (trade_note or "").strip() or None
    try:
        async with db.begin():
            await svc.update_entry(
                db,
                entry_id=entry_id,
                position=position,
                tier=tier_int,
                round=round_int,
                team_id=team_id_int,
                original_team_id=orig_team_id_int,
                trade_note=trade_note_str,
            )
    except svc.BoardError as exc:
        return _redirect_with_error(board_id, str(exc))

    return RedirectResponse(
        url=f"/admin/boards/{board_id}?success=entry_updated", status_code=303
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
        "boards",
        need_edit=True,
        next_path=f"/admin/boards/{board_id}",
    )
    if redirect:
        return redirect
    assert user is not None

    try:
        async with db.begin():
            await svc.delete_entry(db, entry_id=entry_id)
    except svc.BoardError as exc:
        return _redirect_with_error(board_id, str(exc))

    return RedirectResponse(
        url=f"/admin/boards/{board_id}?success=entry_deleted", status_code=303
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
        "boards",
        need_edit=True,
        next_path=f"/admin/boards/{board_id}",
    )
    if redirect:
        return redirect
    assert user is not None

    try:
        async with db.begin():
            await svc.approve_board(db, board_id=board_id)
    except svc.BoardError as exc:
        return _redirect_with_error(board_id, str(exc))

    return RedirectResponse(
        url=f"/admin/boards/{board_id}?success=approved", status_code=303
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
        "boards",
        need_edit=True,
        next_path=f"/admin/boards/{board_id}",
    )
    if redirect:
        return redirect
    assert user is not None

    try:
        async with db.begin():
            await svc.reject_board(db, board_id=board_id)
    except svc.BoardError as exc:
        return _redirect_with_error(board_id, str(exc))

    return RedirectResponse(
        url=f"/admin/boards/{board_id}?success=rejected", status_code=303
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
        "boards",
        need_edit=True,
        next_path=f"/admin/boards/{board_id}",
    )
    if redirect:
        return redirect
    assert user is not None

    try:
        async with db.begin():
            await svc.delete_board(db, board_id=board_id)
    except svc.BoardError as exc:
        return _redirect_with_error(board_id, str(exc))

    return RedirectResponse(url="/admin/boards?success=deleted", status_code=303)


@router.post("/{board_id}/update-meta", response_class=HTMLResponse)
async def update_board_meta(
    request: Request,
    board_id: int,
    news_source_id: int = Form(...),
    draft_year: int = Form(...),
    published_at: str = Form(...),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Patch source / draft year / published_at on a PENDING board."""
    redirect, user = await require_dataset_access(
        request,
        db,
        "boards",
        need_edit=True,
        next_path=f"/admin/boards/{board_id}",
    )
    if redirect:
        return redirect
    assert user is not None

    try:
        parsed = datetime.fromisoformat(published_at)
    except ValueError:
        return _redirect_with_error(
            board_id,
            "Published-at must be an ISO date/time (e.g., 2026-05-23).",
        )
    published_dt = parsed.replace(tzinfo=None) if parsed.tzinfo else parsed

    try:
        async with db.begin():
            await svc.update_board_metadata(
                db,
                board_id=board_id,
                news_source_id=news_source_id,
                draft_year=draft_year,
                published_at=published_dt,
            )
    except svc.BoardError as exc:
        return _redirect_with_error(board_id, str(exc))

    return RedirectResponse(
        url=f"/admin/boards/{board_id}?success=meta_updated",
        status_code=303,
    )


@router.post("/{board_id}/reopen", response_class=HTMLResponse)
async def reopen_big_board(
    request: Request,
    board_id: int,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Reopen an APPROVED board so it can be edited again."""
    redirect, user = await require_dataset_access(
        request,
        db,
        "boards",
        need_edit=True,
        next_path=f"/admin/boards/{board_id}",
    )
    if redirect:
        return redirect
    assert user is not None

    try:
        async with db.begin():
            await svc.reopen_board(db, board_id=board_id)
    except svc.BoardError as exc:
        return _redirect_with_error(board_id, str(exc))

    return RedirectResponse(
        url=f"/admin/boards/{board_id}?success=reopened", status_code=303
    )


@router.post("/{board_id}/clone", response_class=HTMLResponse)
async def clone_big_board(
    request: Request,
    board_id: int,
    published_at: str | None = Form(default=None),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Clone a board into a new PENDING copy with the same entries.

    The caller supplies a ``published_at`` so the clone records when the
    cloned-from analyst published this iteration; defaults to today.
    """
    redirect, user = await require_dataset_access(
        request,
        db,
        "boards",
        need_edit=True,
        next_path=f"/admin/boards/{board_id}",
    )
    if redirect:
        return redirect
    assert user is not None

    published_dt = datetime.utcnow()
    if published_at:
        try:
            parsed = datetime.fromisoformat(published_at)
        except ValueError:
            return _redirect_with_error(
                board_id,
                "Published-at must be an ISO date/time (e.g., 2026-05-23).",
            )
        published_dt = parsed.replace(tzinfo=None) if parsed.tzinfo else parsed

    try:
        async with db.begin():
            clone = await svc.clone_board(
                db, board_id=board_id, published_at=published_dt
            )
    except svc.BoardError as exc:
        return _redirect_with_error(board_id, str(exc))

    return RedirectResponse(
        url=f"/admin/boards/{clone.id}?success=cloned", status_code=303
    )


@router.post("/{board_id}/entries/{entry_id}/move-up", response_class=HTMLResponse)
async def move_entry_up(
    request: Request,
    board_id: int,
    entry_id: int,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Swap an entry with the one ranked immediately above it."""
    return await _move_entry(request, db, board_id, entry_id, "up")


@router.post("/{board_id}/entries/{entry_id}/move-down", response_class=HTMLResponse)
async def move_entry_down(
    request: Request,
    board_id: int,
    entry_id: int,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Swap an entry with the one ranked immediately below it."""
    return await _move_entry(request, db, board_id, entry_id, "down")


async def _move_entry(
    request: Request,
    db: AsyncSession,
    board_id: int,
    entry_id: int,
    direction: str,
) -> Response:
    redirect, user = await require_dataset_access(
        request,
        db,
        "boards",
        need_edit=True,
        next_path=f"/admin/boards/{board_id}",
    )
    if redirect:
        return redirect
    assert user is not None

    try:
        async with db.begin():
            await svc.move_entry(db, entry_id=entry_id, direction=direction)
    except svc.BoardError as exc:
        return _redirect_with_error(board_id, str(exc))

    return RedirectResponse(url=f"/admin/boards/{board_id}", status_code=303)


@router.post("/{board_id}/entries/{entry_id}/assign", response_class=HTMLResponse)
async def assign_board_entry(
    request: Request,
    board_id: int,
    entry_id: int,
    player_id: int = Form(...),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Manually assign a player to an unresolved entry.

    Sets ``player_id`` on the entry and stamps
    ``resolution_method=MANUAL``.  The board must be PENDING.
    Redirects back to the board detail page on success.
    """
    redirect, user = await require_dataset_access(
        request,
        db,
        "boards",
        need_edit=True,
        next_path=f"/admin/boards/{board_id}",
    )
    if redirect:
        return redirect
    assert user is not None

    try:
        async with db.begin():
            await svc.assign_entry(db, entry_id=entry_id, player_id=player_id)
    except svc.BoardError as exc:
        return _redirect_with_error(board_id, str(exc))

    return RedirectResponse(
        url=f"/admin/boards/{board_id}?success=entry_updated", status_code=303
    )


@router.post("/{board_id}/entries/{entry_id}/mint-stub", response_class=HTMLResponse)
async def mint_stub_player(
    request: Request,
    board_id: int,
    entry_id: int,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Create a stub PlayerMaster from an unresolved entry's raw_name.

    Mints a new ``PlayerMaster`` row with ``is_stub=True`` and assigns
    it to the entry with ``resolution_method=STUB``.  The board must be
    PENDING.  Redirects back to the board detail page on success.
    """
    redirect, user = await require_dataset_access(
        request,
        db,
        "boards",
        need_edit=True,
        next_path=f"/admin/boards/{board_id}",
    )
    if redirect:
        return redirect
    assert user is not None

    try:
        async with db.begin():
            await svc.mint_stub_for_entry(db, entry_id=entry_id)
    except svc.BoardError as exc:
        return _redirect_with_error(board_id, str(exc))

    return RedirectResponse(
        url=f"/admin/boards/{board_id}?success=stub_minted", status_code=303
    )


@router.get("/{board_id}/entries/player-search", response_class=JSONResponse)
async def board_player_search(
    request: Request,
    board_id: int,
    q: str = Query(default="", min_length=0),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Return up to 10 players matching *q* by trigram similarity.

    Used by the free-form search input on unresolved entries.  Results are
    JSON-serialised ``[{id, display_name, school}, ...]`` matching the
    shape consumed by the autocomplete helper in ``boards-detail.js``.
    """
    redirect, user = await require_dataset_access(
        request,
        db,
        "boards",
        need_edit=False,
        next_path=f"/admin/boards/{board_id}",
    )
    if redirect:
        return JSONResponse(content=[], status_code=401)
    assert user is not None

    q_stripped = q.strip()
    if len(q_stripped) < 2:
        return JSONResponse(content=[])

    candidates = await find_lexical_players(db, q_stripped, k=10)
    return JSONResponse(
        content=[
            {
                "id": c.player_id,
                "display_name": c.display_name or "",
                "school": c.school or "",
            }
            for c in candidates
        ]
    )


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
        url=f"/admin/boards/{board_id}?error={quote(message)}",
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
        "admin/boards/new.html",
        await base_context_with_permissions(
            request,
            db,
            user,  # type: ignore[arg-type]
            sources=sources,
            default_draft_year=datetime.utcnow().year + 1,
            error=message,
        ),
    )
