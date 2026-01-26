"""Admin player image asset routes.

Provides list, detail, and delete for player image assets.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app.routes.admin.helpers import base_context, require_admin
from app.services.admin_image_service import (
    delete_image_asset,
    get_image_asset,
    get_image_snapshot,
    list_player_images,
)
from app.services.admin_player_service import get_player_by_id
from app.utils.db_async import get_session

router = APIRouter(prefix="/players", tags=["admin-player-images"])

# Default pagination values
DEFAULT_LIMIT = 20
MAX_LIMIT = 100


@router.get("/{player_id}/images", response_class=HTMLResponse)
async def list_images(
    request: Request,
    player_id: int,
    success: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """List all image assets for a player (admin only)."""
    redirect, user = await require_admin(request, db)
    if redirect:
        return redirect

    player = await get_player_by_id(db, player_id)
    if player is None:
        raise HTTPException(status_code=404, detail="Player not found")

    result = await list_player_images(db, player_id, limit, offset)

    # Calculate pagination info
    pages = (result.total + limit - 1) // limit if result.total > 0 else 1
    current_page = (offset // limit) + 1

    success_message = None
    warning_message = None
    if success == "deleted":
        success_message = "Image deleted successfully."
    elif success == "deleted_db_only":
        warning_message = "Image record deleted, but storage file could not be removed."

    return request.app.state.templates.TemplateResponse(
        "admin/players/images/index.html",
        base_context(
            request,
            user=user,
            player=player,
            images=result.images,
            total=result.total,
            limit=limit,
            offset=offset,
            pages=pages,
            current_page=current_page,
            success=success_message,
            warning=warning_message,
            active_nav="players",
        ),
    )


@router.get("/{player_id}/images/{asset_id}", response_class=HTMLResponse)
async def view_image(
    request: Request,
    player_id: int,
    asset_id: int,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """View details of a specific image asset (admin only)."""
    redirect, user = await require_admin(request, db)
    if redirect:
        return redirect

    player = await get_player_by_id(db, player_id)
    if player is None:
        raise HTTPException(status_code=404, detail="Player not found")

    asset = await get_image_asset(db, asset_id)
    if asset is None or asset.player_id != player_id:
        raise HTTPException(status_code=404, detail="Image not found")

    # Get the snapshot for additional context
    snapshot = await get_image_snapshot(db, asset.snapshot_id)

    return request.app.state.templates.TemplateResponse(
        "admin/players/images/detail.html",
        base_context(
            request,
            user=user,
            player=player,
            image=asset,
            snapshot=snapshot,
            active_nav="players",
        ),
    )


@router.post("/{player_id}/images/{asset_id}/delete", response_class=HTMLResponse)
async def delete_image(
    request: Request,
    player_id: int,
    asset_id: int,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Delete an image asset (admin only)."""
    redirect, user = await require_admin(request, db)
    if redirect:
        return redirect

    player = await get_player_by_id(db, player_id)
    if player is None:
        raise HTTPException(status_code=404, detail="Player not found")

    asset = await get_image_asset(db, asset_id)
    if asset is None or asset.player_id != player_id:
        raise HTTPException(status_code=404, detail="Image not found")

    async with db.begin():
        storage_deleted = await delete_image_asset(db, asset, delete_from_storage=True)

    # Use different success param based on whether storage deletion succeeded
    success_param = "deleted" if storage_deleted else "deleted_db_only"

    return RedirectResponse(
        url=f"/admin/players/{player_id}/images?success={success_param}",
        status_code=303,
    )
