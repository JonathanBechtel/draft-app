"""Admin PlayerAlias CRUD routes.

Provides list, create, read, update, and delete for player alias records.
Routes are thin wrappers; business logic lives in admin_player_related_service.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app.routes.admin.helpers import base_context, require_admin
from app.schemas.auth import AuthUser
from app.schemas.player_aliases import PlayerAlias
from app.services.admin_player_related_service import (
    PlayerAliasFormData,
    check_alias_uniqueness,
    create_player_alias as svc_create_alias,
    delete_player_alias as svc_delete_alias,
    get_player_alias_by_id,
    list_player_aliases,
    parse_alias_form,
    update_player_alias as svc_update_alias,
    validate_alias_form,
)
from app.services.admin_player_service import get_player_by_id
from app.utils.db_async import get_session

router = APIRouter(prefix="/players/{player_id}/aliases", tags=["admin-player-aliases"])

SUCCESS_MESSAGES = {
    "created": "Alias created successfully.",
    "updated": "Alias updated successfully.",
    "deleted": "Alias deleted successfully.",
}


def _build_form_data(
    full_name: str,
    prefix: str | None,
    first_name: str | None,
    middle_name: str | None,
    last_name: str | None,
    suffix: str | None,
    context: str | None,
) -> PlayerAliasFormData:
    """Build PlayerAliasFormData from individual form fields."""
    return PlayerAliasFormData(
        full_name=full_name,
        prefix=prefix,
        first_name=first_name,
        middle_name=middle_name,
        last_name=last_name,
        suffix=suffix,
        context=context,
    )


def _render_form_error(
    request: Request,
    user: AuthUser | None,
    player_id: int,
    player_name: str,
    alias: PlayerAlias | None,
    error: str,
) -> Response:
    """Render create/edit form with an error message."""
    template = (
        "admin/players/aliases/detail.html"
        if alias
        else "admin/players/aliases/form.html"
    )
    return request.app.state.templates.TemplateResponse(
        template,
        base_context(
            request,
            user=user,
            player_id=player_id,
            player_name=player_name,
            alias=alias,
            error=error,
            active_nav="players",
        ),
    )


@router.get("", response_class=HTMLResponse)
async def list_aliases(
    request: Request,
    player_id: int,
    success: str | None = Query(default=None),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """List all aliases for a player (admin only)."""
    redirect, user = await require_admin(request, db)
    if redirect:
        return redirect

    player = await get_player_by_id(db, player_id)
    if player is None:
        raise HTTPException(status_code=404, detail="Player not found")

    aliases = await list_player_aliases(db, player_id)

    return request.app.state.templates.TemplateResponse(
        "admin/players/aliases/index.html",
        base_context(
            request,
            user=user,
            player_id=player_id,
            player_name=player.display_name or "",
            aliases=aliases,
            total=len(aliases),
            success=SUCCESS_MESSAGES.get(success) if success else None,
            active_nav="players",
        ),
    )


@router.get("/new", response_class=HTMLResponse)
async def new_alias(
    request: Request,
    player_id: int,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Display the create alias form (admin only)."""
    redirect, user = await require_admin(request, db)
    if redirect:
        return redirect

    player = await get_player_by_id(db, player_id)
    if player is None:
        raise HTTPException(status_code=404, detail="Player not found")

    return request.app.state.templates.TemplateResponse(
        "admin/players/aliases/form.html",
        base_context(
            request,
            user=user,
            player_id=player_id,
            player_name=player.display_name or "",
            alias=None,
            error=None,
            active_nav="players",
        ),
    )


@router.post("", response_class=HTMLResponse)
async def create_alias(
    request: Request,
    player_id: int,
    full_name: str = Form(...),
    prefix: str | None = Form(default=None),
    first_name: str | None = Form(default=None),
    middle_name: str | None = Form(default=None),
    last_name: str | None = Form(default=None),
    suffix: str | None = Form(default=None),
    context: str | None = Form(default=None),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Create a new player alias (admin only)."""
    redirect, user = await require_admin(request, db)
    if redirect:
        return redirect

    form_data = _build_form_data(
        full_name, prefix, first_name, middle_name, last_name, suffix, context
    )

    # Validate form data (no DB needed)
    if error := validate_alias_form(form_data):
        async with db.begin():
            player = await get_player_by_id(db, player_id)
            if player is None:
                raise HTTPException(status_code=404, detail="Player not found")
            player_name = player.display_name or ""
        return _render_form_error(request, user, player_id, player_name, None, error)

    # Parse form data (no DB needed)
    parsed = parse_alias_form(form_data)
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

        # Check uniqueness
        is_unique = await check_alias_uniqueness(db, player_id, parsed.full_name)
        if not is_unique:
            return _render_form_error(
                request,
                user,
                player_id,
                player_name,
                None,
                f"An alias with name '{parsed.full_name}' already exists for this player.",
            )

        await svc_create_alias(db, player_id, parsed)

    return RedirectResponse(
        url=f"/admin/players/{player_id}/aliases?success=created",
        status_code=303,
    )


@router.get("/{alias_id}", response_class=HTMLResponse)
async def edit_alias(
    request: Request,
    player_id: int,
    alias_id: int,
    success: str | None = Query(default=None),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Display the edit alias form (admin only)."""
    redirect, user = await require_admin(request, db)
    if redirect:
        return redirect

    player = await get_player_by_id(db, player_id)
    if player is None:
        raise HTTPException(status_code=404, detail="Player not found")

    alias = await get_player_alias_by_id(db, alias_id)
    if alias is None or alias.player_id != player_id:
        raise HTTPException(status_code=404, detail="Alias not found")

    return request.app.state.templates.TemplateResponse(
        "admin/players/aliases/detail.html",
        base_context(
            request,
            user=user,
            player_id=player_id,
            player_name=player.display_name or "",
            alias=alias,
            success=SUCCESS_MESSAGES.get(success) if success else None,
            active_nav="players",
        ),
    )


@router.post("/{alias_id}", response_class=HTMLResponse)
async def update_alias(
    request: Request,
    player_id: int,
    alias_id: int,
    full_name: str = Form(...),
    prefix: str | None = Form(default=None),
    first_name: str | None = Form(default=None),
    middle_name: str | None = Form(default=None),
    last_name: str | None = Form(default=None),
    suffix: str | None = Form(default=None),
    context: str | None = Form(default=None),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Update a player alias (admin only)."""
    redirect, user = await require_admin(request, db)
    if redirect:
        return redirect

    form_data = _build_form_data(
        full_name, prefix, first_name, middle_name, last_name, suffix, context
    )

    # All DB operations in single transaction
    async with db.begin():
        player = await get_player_by_id(db, player_id)
        if player is None:
            raise HTTPException(status_code=404, detail="Player not found")

        alias = await get_player_alias_by_id(db, alias_id)
        if alias is None or alias.player_id != player_id:
            raise HTTPException(status_code=404, detail="Alias not found")

        player_name = player.display_name or ""

        # Validate
        if error := validate_alias_form(form_data):
            return _render_form_error(
                request, user, player_id, player_name, alias, error
            )

        # Parse
        parsed = parse_alias_form(form_data)
        if isinstance(parsed, str):
            return _render_form_error(
                request, user, player_id, player_name, alias, parsed
            )

        # Check uniqueness (exclude current alias)
        is_unique = await check_alias_uniqueness(
            db, player_id, parsed.full_name, exclude_id=alias_id
        )
        if not is_unique:
            return _render_form_error(
                request,
                user,
                player_id,
                player_name,
                alias,
                f"An alias with name '{parsed.full_name}' already exists for this player.",
            )

        await svc_update_alias(db, alias, parsed)

    return RedirectResponse(
        url=f"/admin/players/{player_id}/aliases/{alias_id}?success=updated",
        status_code=303,
    )


@router.post("/{alias_id}/delete", response_class=HTMLResponse)
async def delete_alias(
    request: Request,
    player_id: int,
    alias_id: int,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Delete a player alias (admin only)."""
    redirect, user = await require_admin(request, db)
    if redirect:
        return redirect

    async with db.begin():
        player = await get_player_by_id(db, player_id)
        if player is None:
            raise HTTPException(status_code=404, detail="Player not found")

        alias = await get_player_alias_by_id(db, alias_id)
        if alias is None or alias.player_id != player_id:
            raise HTTPException(status_code=404, detail="Alias not found")
        await svc_delete_alias(db, alias)

    return RedirectResponse(
        url=f"/admin/players/{player_id}/aliases?success=deleted",
        status_code=303,
    )
