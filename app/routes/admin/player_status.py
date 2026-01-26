"""Admin PlayerStatus routes.

Provides view/edit form and upsert/delete for player status records.
Routes are thin wrappers; business logic lives in admin_player_related_service.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app.routes.admin.helpers import base_context, require_admin
from app.schemas.auth import AuthUser
from app.schemas.player_status import PlayerStatus
from app.services.admin_player_related_service import (
    PlayerStatusFormData,
    delete_player_status as svc_delete_status,
    get_all_positions,
    get_player_status,
    parse_status_form,
    upsert_player_status,
    validate_status_form,
)
from app.services.admin_player_service import get_player_by_id
from app.utils.db_async import get_session

router = APIRouter(prefix="/players/{player_id}/status", tags=["admin-player-status"])

SUCCESS_MESSAGES = {
    "saved": "Status saved successfully.",
    "deleted": "Status deleted successfully.",
}


def _render_form_error(
    request: Request,
    user: AuthUser | None,
    player_id: int,
    player_name: str,
    status: PlayerStatus | None,
    positions: list,
    error: str,
) -> Response:
    """Render status form with an error message."""
    return request.app.state.templates.TemplateResponse(
        "admin/players/status/form.html",
        base_context(
            request,
            user=user,
            player_id=player_id,
            player_name=player_name,
            status=status,
            positions=positions,
            error=error,
            active_nav="players",
        ),
    )


@router.get("", response_class=HTMLResponse)
async def edit_status(
    request: Request,
    player_id: int,
    success: str | None = Query(default=None),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Display the player status edit/create form (admin only)."""
    redirect, user = await require_admin(request, db)
    if redirect:
        return redirect

    player = await get_player_by_id(db, player_id)
    if player is None:
        raise HTTPException(status_code=404, detail="Player not found")

    status = await get_player_status(db, player_id)
    positions = await get_all_positions(db)

    return request.app.state.templates.TemplateResponse(
        "admin/players/status/form.html",
        base_context(
            request,
            user=user,
            player_id=player_id,
            player_name=player.display_name,
            status=status,
            positions=positions,
            success=SUCCESS_MESSAGES.get(success) if success else None,
            active_nav="players",
        ),
    )


@router.post("", response_class=HTMLResponse)
async def save_status(
    request: Request,
    player_id: int,
    position_id: str | None = Form(default=None),
    is_active_nba: str | None = Form(default=None),
    current_team: str | None = Form(default=None),
    nba_last_season: str | None = Form(default=None),
    raw_position: str | None = Form(default=None),
    height_in: str | None = Form(default=None),
    weight_lb: str | None = Form(default=None),
    source: str | None = Form(default=None),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Create or update player status (admin only)."""
    redirect, user = await require_admin(request, db)
    if redirect:
        return redirect

    form_data = PlayerStatusFormData(
        position_id=position_id,
        is_active_nba=is_active_nba,
        current_team=current_team,
        nba_last_season=nba_last_season,
        raw_position=raw_position,
        height_in=height_in,
        weight_lb=weight_lb,
        source=source,
    )

    # Validate form data (no DB needed)
    if error := validate_status_form(form_data):
        async with db.begin():
            player = await get_player_by_id(db, player_id)
            if player is None:
                raise HTTPException(status_code=404, detail="Player not found")
            player_name = player.display_name or ""
            status = await get_player_status(db, player_id)
            positions = await get_all_positions(db)
        return _render_form_error(
            request, user, player_id, player_name, status, positions, error
        )

    # Parse form data (no DB needed)
    parsed = parse_status_form(form_data)
    if isinstance(parsed, str):
        async with db.begin():
            player = await get_player_by_id(db, player_id)
            if player is None:
                raise HTTPException(status_code=404, detail="Player not found")
            player_name = player.display_name or ""
            status = await get_player_status(db, player_id)
            positions = await get_all_positions(db)
        return _render_form_error(
            request, user, player_id, player_name, status, positions, parsed
        )

    # All DB operations in single transaction
    async with db.begin():
        player = await get_player_by_id(db, player_id)
        if player is None:
            raise HTTPException(status_code=404, detail="Player not found")

        await upsert_player_status(db, player_id, parsed)

    return RedirectResponse(
        url=f"/admin/players/{player_id}/status?success=saved",
        status_code=303,
    )


@router.post("/delete", response_class=HTMLResponse)
async def delete_status(
    request: Request,
    player_id: int,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Delete player status (admin only)."""
    redirect, user = await require_admin(request, db)
    if redirect:
        return redirect

    async with db.begin():
        player = await get_player_by_id(db, player_id)
        if player is None:
            raise HTTPException(status_code=404, detail="Player not found")

        status = await get_player_status(db, player_id)
        if status is None:
            raise HTTPException(status_code=404, detail="Status not found")
        await svc_delete_status(db, status)

    return RedirectResponse(
        url=f"/admin/players/{player_id}?success=status_deleted",
        status_code=303,
    )
