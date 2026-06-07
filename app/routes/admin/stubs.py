"""Admin Stub Player management routes.

Provides list, quick-add, promote, and reference-guarded delete for stub
player records (``PlayerMaster.is_stub = True``).

Routes are thin wrappers; business logic lives in admin_player_service and
player_mention_service.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app.routes.admin.helpers import (
    base_context_with_permissions,
    require_dataset_access,
)
from app.services.admin_player_service import (
    PlayerListResult,
    delete_stub as svc_delete_stub,
    get_player_by_id,
    list_players as svc_list_players,
    promote_stub_to_full as svc_promote_stub,
)
from app.services.player_mention_service import create_stub_player
from app.utils.db_async import get_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/players/stubs", tags=["admin-stubs"])

DEFAULT_LIMIT = 25
MAX_LIMIT = 100

_NEXT_PATH = "/admin/players/stubs"

SUCCESS_MESSAGES = {
    "quick_added": "Stub player created successfully.",
    "promoted": "Player promoted to full record.",
    "deleted": "Stub player deleted.",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _enrichment_status_label(
    enrichment_attempted_at: object | None,
    latest_job_state: str | None,
) -> str:
    """Derive a human-readable enrichment status label.

    Args:
        enrichment_attempted_at: Timestamp from PlayerMaster, or None.
        latest_job_state: Most-recent PlayerEnrichmentJob state, or None.

    Returns:
        One of: "Not attempted", "Enriching…", "Enriched", "Failed".
    """
    if latest_job_state in ("queued", "running"):
        return "Enriching…"
    if latest_job_state == "failed":
        return "Failed"
    if enrichment_attempted_at is not None:
        return "Enriched"
    return "Not attempted"


async def _render_stubs_list(
    request: Request,
    db: AsyncSession,
    user: object,
    list_result: PlayerListResult,
    *,
    limit: int,
    offset: int,
    q: str | None,
    draft_year: int | None,
    enrichment_status: str | None,
    error: str | None,
    success: str | None,
) -> Response:
    """Render the stubs list template with pagination context."""
    pages = (list_result.total + limit - 1) // limit if list_result.total > 0 else 1
    current_page = (offset // limit) + 1

    return request.app.state.templates.TemplateResponse(
        "admin/players/stubs.html",
        await base_context_with_permissions(
            request,
            db,
            user,  # type: ignore[arg-type]
            players=list_result.players,
            total=list_result.total,
            limit=limit,
            offset=offset,
            pages=pages,
            current_page=current_page,
            q=q,
            draft_year=draft_year,
            draft_years=list_result.draft_years,
            enrichment_status=enrichment_status,
            enrichment_status_label=_enrichment_status_label,
            error=error,
            success=success,
            active_nav="stubs",
        ),
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
async def list_stubs(
    request: Request,
    success: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    q: str | None = Query(default=None),
    draft_year: str | None = Query(default=None),
    enrichment_status: str | None = Query(default=None),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """List stub players with filters and pagination.

    Args:
        request: Incoming FastAPI request.
        success: Flash-message key from a redirect after a successful action.
        limit: Page size.
        offset: Page offset.
        q: Free-text name search.
        draft_year: Filter by draft year.
        enrichment_status: Filter by enrichment status label.
        db: Injected database session.

    Returns:
        Rendered HTML page.
    """
    redirect, user = await require_dataset_access(
        request, db, "players", need_edit=False, next_path=_NEXT_PATH
    )
    if redirect:
        return redirect
    assert user is not None

    draft_year_int: int | None = None
    if draft_year and draft_year.strip():
        try:
            draft_year_int = int(draft_year.strip())
        except ValueError:
            draft_year_int = None

    result = await svc_list_players(
        db,
        q,
        draft_year_int,
        None,
        None,
        None,
        limit,
        offset,
        is_stub=True,
        enrichment_status=enrichment_status,
    )

    return await _render_stubs_list(
        request,
        db,
        user,
        result,
        limit=limit,
        offset=offset,
        q=q,
        draft_year=draft_year_int,
        enrichment_status=enrichment_status,
        error=None,
        success=SUCCESS_MESSAGES.get(success) if success else None,
    )


@router.post("/quick-add", response_class=HTMLResponse)
async def quick_add_stub(
    request: Request,
    display_name: str = Form(...),
    draft_year: str | None = Form(default=None),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Create a stub player via the name-only quick-add modal.

    Runs dedup pre-check via :func:`~app.services.player_mention_service.create_stub_player`
    and surfaces blocked/ambiguous/guard outcomes as flash messages.

    Args:
        request: Incoming FastAPI request.
        display_name: The player's full name.
        draft_year: Optional draft year string.
        db: Injected database session.

    Returns:
        Redirect to the stubs list, or re-rendered list with error.
    """
    redirect, user = await require_dataset_access(
        request, db, "players", need_edit=True, next_path=_NEXT_PATH
    )
    if redirect:
        return redirect
    assert user is not None

    draft_year_int: int | None = None
    if draft_year and draft_year.strip():
        try:
            draft_year_int = int(draft_year.strip())
        except ValueError:
            draft_year_int = None

    async with db.begin():
        result_obj = await create_stub_player(
            db, display_name.strip(), draft_year=draft_year_int
        )

    if result_obj.outcome == "created":
        return RedirectResponse(
            url=f"{_NEXT_PATH}?success=quick_added",
            status_code=303,
        )

    # Surface outcomes as errors on re-rendered list
    if result_obj.outcome == "blocked_existing":
        match = result_obj.match
        error_msg = (
            f"A player named '{match.display_name}' already exists "
            f"(id {match.player_id}). Open it or add an alias instead."
            if match
            else "A matching player already exists."
        )
    elif result_obj.outcome == "ambiguous":
        candidates_text = ", ".join(
            f"'{c.display_name}' (id {c.player_id})"
            for c in (result_obj.candidates or [])[:5]
        )
        error_msg = (
            f"Ambiguous name — multiple existing matches: {candidates_text}. "
            "Select the correct player or use a more specific name."
        )
    else:
        # rejected_guard
        error_msg = f"Cannot create stub: {result_obj.reason or 'name is too vague'}"

    list_result = await svc_list_players(
        db, None, None, None, None, None, DEFAULT_LIMIT, 0, is_stub=True
    )
    return await _render_stubs_list(
        request,
        db,
        user,
        list_result,
        limit=DEFAULT_LIMIT,
        offset=0,
        q=None,
        draft_year=None,
        enrichment_status=None,
        error=error_msg,
        success=None,
    )


@router.post("/{player_id}/promote", response_class=HTMLResponse)
async def promote_stub(
    request: Request,
    player_id: int,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Promote a stub player to a full player record.

    Clears the ``is_stub`` flag on the PlayerMaster row.

    Args:
        request: Incoming FastAPI request.
        player_id: ID of the stub to promote.
        db: Injected database session.

    Returns:
        Redirect to the stubs list or 404.
    """
    redirect, user = await require_dataset_access(
        request, db, "players", need_edit=True, next_path=_NEXT_PATH
    )
    if redirect:
        return redirect
    assert user is not None

    async with db.begin():
        player = await get_player_by_id(db, player_id)
        if player is None:
            raise HTTPException(status_code=404, detail="Player not found")
        if not player.is_stub:
            raise HTTPException(status_code=400, detail="Player is not a stub")
        await svc_promote_stub(db, player_id)

    return RedirectResponse(url=f"{_NEXT_PATH}?success=promoted", status_code=303)


@router.post("/{player_id}/delete", response_class=HTMLResponse)
async def delete_stub(
    request: Request,
    player_id: int,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Delete an orphan stub player.

    Refuses deletion when the stub has non-trivial inbound references (e.g.,
    board entries, news mentions).  Safe child rows (lifecycle, aliases)
    are cleaned up automatically.

    Args:
        request: Incoming FastAPI request.
        player_id: ID of the stub to delete.
        db: Injected database session.

    Returns:
        Redirect to the stubs list, or list page with error if guarded.
    """
    redirect, user = await require_dataset_access(
        request, db, "players", need_edit=True, next_path=_NEXT_PATH
    )
    if redirect:
        return redirect
    assert user is not None

    async with db.begin():
        player = await get_player_by_id(db, player_id)
        if player is None:
            raise HTTPException(status_code=404, detail="Player not found")

        try:
            await svc_delete_stub(db, player_id)
        except ValueError as exc:
            list_result = await svc_list_players(
                db, None, None, None, None, None, DEFAULT_LIMIT, 0, is_stub=True
            )
            return await _render_stubs_list(
                request,
                db,
                user,
                list_result,
                limit=DEFAULT_LIMIT,
                offset=0,
                q=None,
                draft_year=None,
                enrichment_status=None,
                error=str(exc),
                success=None,
            )

    return RedirectResponse(url=f"{_NEXT_PATH}?success=deleted", status_code=303)


@router.post("/bulk-delete", response_class=HTMLResponse)
async def bulk_delete_stubs(
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Bulk-delete selected stub players.

    Reads ``player_ids[]`` from the form body.  Skips players that cannot be
    deleted (inbound references) and surfaces per-player errors.

    Args:
        request: Incoming FastAPI request.
        db: Injected database session.

    Returns:
        Redirect to the stubs list with a summary flash message.
    """
    redirect, user = await require_dataset_access(
        request, db, "players", need_edit=True, next_path=_NEXT_PATH
    )
    if redirect:
        return redirect
    assert user is not None

    form = await request.form()
    raw_ids = form.getlist("player_ids[]")
    player_ids: list[int] = []
    for raw in raw_ids:
        try:
            player_ids.append(int(str(raw)))
        except ValueError:
            pass

    # Eagerly read user id before it expires across transaction boundaries
    user_id = user.id  # type: ignore[union-attr]

    deleted = 0
    errors: list[str] = []
    for pid in player_ids:
        try:
            async with db.begin():
                await svc_delete_stub(db, pid)
            deleted += 1
        except ValueError as exc:
            errors.append(str(exc))
        except Exception as exc:
            logger.warning("Unexpected error deleting stub %d: %s", pid, exc)
            errors.append(f"Unexpected error for player {pid}")

    if errors:
        error_summary = f"Deleted {deleted}; {len(errors)} failed: " + "; ".join(
            errors[:3]
        )
        async with db.begin():
            list_result = await svc_list_players(
                db, None, None, None, None, None, DEFAULT_LIMIT, 0, is_stub=True
            )
            # Reload the user within the active transaction to avoid expired-ORM issues
            from sqlalchemy import select as sa_select
            from app.schemas.auth import AuthUser

            refreshed_user_result = await db.execute(
                sa_select(AuthUser).where(  # type: ignore[call-overload]
                    AuthUser.id == user_id  # type: ignore[arg-type]
                )
            )
            refreshed_user = refreshed_user_result.scalar_one_or_none() or user
            return await _render_stubs_list(
                request,
                db,
                refreshed_user,
                list_result,
                limit=DEFAULT_LIMIT,
                offset=0,
                q=None,
                draft_year=None,
                enrichment_status=None,
                error=error_summary,
                success=None,
            )

    return RedirectResponse(url=f"{_NEXT_PATH}?success=deleted", status_code=303)
