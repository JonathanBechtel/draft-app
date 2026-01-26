"""Admin PlayerExternalId CRUD routes.

Provides list, create, read, update, and delete for player external ID records.
Routes are thin wrappers; business logic lives in admin_player_related_service.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app.routes.admin.helpers import base_context, require_admin
from app.schemas.auth import AuthUser
from app.schemas.player_external_ids import PlayerExternalId
from app.services.admin_player_related_service import (
    PlayerExternalIdFormData,
    check_external_id_uniqueness,
    create_player_external_id as svc_create_ext_id,
    delete_player_external_id as svc_delete_ext_id,
    get_player_external_id_by_id,
    list_player_external_ids,
    parse_external_id_form,
    update_player_external_id as svc_update_ext_id,
    validate_external_id_form,
)
from app.services.admin_player_service import get_player_by_id
from app.utils.db_async import get_session

router = APIRouter(
    prefix="/players/{player_id}/external-ids", tags=["admin-player-external-ids"]
)

SUCCESS_MESSAGES = {
    "created": "External ID created successfully.",
    "updated": "External ID updated successfully.",
    "deleted": "External ID deleted successfully.",
}


def _build_form_data(
    system: str,
    external_id: str,
    source_url: str | None,
) -> PlayerExternalIdFormData:
    """Build PlayerExternalIdFormData from individual form fields."""
    return PlayerExternalIdFormData(
        system=system,
        external_id=external_id,
        source_url=source_url,
    )


def _render_form_error(
    request: Request,
    user: AuthUser | None,
    player_id: int,
    player_name: str,
    ext: PlayerExternalId | None,
    error: str,
) -> Response:
    """Render create/edit form with an error message."""
    template = (
        "admin/players/external-ids/detail.html"
        if ext
        else "admin/players/external-ids/form.html"
    )
    return request.app.state.templates.TemplateResponse(
        template,
        base_context(
            request,
            user=user,
            player_id=player_id,
            player_name=player_name,
            ext=ext,
            error=error,
            active_nav="players",
        ),
    )


@router.get("", response_class=HTMLResponse)
async def list_external_ids(
    request: Request,
    player_id: int,
    success: str | None = Query(default=None),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """List all external IDs for a player (admin only)."""
    redirect, user = await require_admin(request, db)
    if redirect:
        return redirect

    player = await get_player_by_id(db, player_id)
    if player is None:
        raise HTTPException(status_code=404, detail="Player not found")

    external_ids = await list_player_external_ids(db, player_id)

    return request.app.state.templates.TemplateResponse(
        "admin/players/external-ids/index.html",
        base_context(
            request,
            user=user,
            player_id=player_id,
            player_name=player.display_name or "",
            external_ids=external_ids,
            total=len(external_ids),
            success=SUCCESS_MESSAGES.get(success) if success else None,
            active_nav="players",
        ),
    )


@router.get("/new", response_class=HTMLResponse)
async def new_external_id(
    request: Request,
    player_id: int,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Display the create external ID form (admin only)."""
    redirect, user = await require_admin(request, db)
    if redirect:
        return redirect

    player = await get_player_by_id(db, player_id)
    if player is None:
        raise HTTPException(status_code=404, detail="Player not found")

    return request.app.state.templates.TemplateResponse(
        "admin/players/external-ids/form.html",
        base_context(
            request,
            user=user,
            player_id=player_id,
            player_name=player.display_name or "",
            ext=None,
            error=None,
            active_nav="players",
        ),
    )


@router.post("", response_class=HTMLResponse)
async def create_external_id(
    request: Request,
    player_id: int,
    system: str = Form(...),
    external_id: str = Form(...),
    source_url: str | None = Form(default=None),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Create a new player external ID (admin only)."""
    redirect, user = await require_admin(request, db)
    if redirect:
        return redirect

    form_data = _build_form_data(system, external_id, source_url)

    # Validate form data (no DB needed)
    if error := validate_external_id_form(form_data):
        async with db.begin():
            player = await get_player_by_id(db, player_id)
            if player is None:
                raise HTTPException(status_code=404, detail="Player not found")
            player_name = player.display_name or ""
        return _render_form_error(request, user, player_id, player_name, None, error)

    # Parse form data (no DB needed)
    parsed = parse_external_id_form(form_data)
    if isinstance(parsed, str):
        async with db.begin():
            player = await get_player_by_id(db, player_id)
            if player is None:
                raise HTTPException(status_code=404, detail="Player not found")
            player_name = player.display_name or ""
        return _render_form_error(request, user, player_id, player_name, None, parsed)

    # All DB operations in single transaction
    async with db.begin():
        player = await get_player_by_id(db, player_id)
        if player is None:
            raise HTTPException(status_code=404, detail="Player not found")

        player_name = player.display_name or ""

        # Check global uniqueness
        is_unique = await check_external_id_uniqueness(
            db, parsed.system, parsed.external_id
        )
        if not is_unique:
            return _render_form_error(
                request,
                user,
                player_id,
                player_name,
                None,
                f"External ID '{parsed.external_id}' already exists for system '{parsed.system}'.",
            )

        await svc_create_ext_id(db, player_id, parsed)

    return RedirectResponse(
        url=f"/admin/players/{player_id}/external-ids?success=created",
        status_code=303,
    )


@router.get("/{ext_id}", response_class=HTMLResponse)
async def edit_external_id(
    request: Request,
    player_id: int,
    ext_id: int,
    success: str | None = Query(default=None),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Display the edit external ID form (admin only)."""
    redirect, user = await require_admin(request, db)
    if redirect:
        return redirect

    player = await get_player_by_id(db, player_id)
    if player is None:
        raise HTTPException(status_code=404, detail="Player not found")

    ext = await get_player_external_id_by_id(db, ext_id)
    if ext is None or ext.player_id != player_id:
        raise HTTPException(status_code=404, detail="External ID not found")

    return request.app.state.templates.TemplateResponse(
        "admin/players/external-ids/detail.html",
        base_context(
            request,
            user=user,
            player_id=player_id,
            player_name=player.display_name or "",
            ext=ext,
            success=SUCCESS_MESSAGES.get(success) if success else None,
            active_nav="players",
        ),
    )


@router.post("/{ext_id}", response_class=HTMLResponse)
async def update_external_id(
    request: Request,
    player_id: int,
    ext_id: int,
    system: str = Form(...),
    external_id: str = Form(...),
    source_url: str | None = Form(default=None),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Update a player external ID (admin only)."""
    redirect, user = await require_admin(request, db)
    if redirect:
        return redirect

    form_data = _build_form_data(system, external_id, source_url)

    # All DB operations in single transaction
    async with db.begin():
        player = await get_player_by_id(db, player_id)
        if player is None:
            raise HTTPException(status_code=404, detail="Player not found")

        ext = await get_player_external_id_by_id(db, ext_id)
        if ext is None or ext.player_id != player_id:
            raise HTTPException(status_code=404, detail="External ID not found")

        player_name = player.display_name or ""

        # Validate
        if error := validate_external_id_form(form_data):
            return _render_form_error(request, user, player_id, player_name, ext, error)

        # Parse
        parsed = parse_external_id_form(form_data)
        if isinstance(parsed, str):
            return _render_form_error(
                request, user, player_id, player_name, ext, parsed
            )

        # Check global uniqueness (exclude current)
        is_unique = await check_external_id_uniqueness(
            db, parsed.system, parsed.external_id, exclude_id=ext_id
        )
        if not is_unique:
            return _render_form_error(
                request,
                user,
                player_id,
                player_name,
                ext,
                f"External ID '{parsed.external_id}' already exists for system '{parsed.system}'.",
            )

        await svc_update_ext_id(db, ext, parsed)

    return RedirectResponse(
        url=f"/admin/players/{player_id}/external-ids/{ext_id}?success=updated",
        status_code=303,
    )


@router.post("/{ext_id}/delete", response_class=HTMLResponse)
async def delete_external_id(
    request: Request,
    player_id: int,
    ext_id: int,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Delete a player external ID (admin only)."""
    redirect, user = await require_admin(request, db)
    if redirect:
        return redirect

    async with db.begin():
        player = await get_player_by_id(db, player_id)
        if player is None:
            raise HTTPException(status_code=404, detail="Player not found")

        ext = await get_player_external_id_by_id(db, ext_id)
        if ext is None or ext.player_id != player_id:
            raise HTTPException(status_code=404, detail="External ID not found")
        await svc_delete_ext_id(db, ext)

    return RedirectResponse(
        url=f"/admin/players/{player_id}/external-ids?success=deleted",
        status_code=303,
    )
