"""Admin image browsing and management routes.

Provides list, detail, delete, and regenerate endpoints for PlayerImageAsset records.
Includes preview/accept flow for image regeneration.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app.routes.admin.helpers import base_context, require_admin
from app.services.admin_image_service import (
    approve_preview as svc_approve_preview,
    create_preview as svc_create_preview,
    delete_image as svc_delete_image,
    delete_preview as svc_delete_preview,
    get_image_by_id,
    get_player_by_id,
    get_preview_by_id,
    list_images as svc_list_images,
)
from app.services.image_generation import image_generation_service
from app.utils.db_async import get_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/images", tags=["admin-images"])

# Default pagination values
DEFAULT_LIMIT = 48  # 6x8 grid
MAX_LIMIT = 100

# Success messages for flash-style notifications
SUCCESS_MESSAGES = {
    "deleted": "Image deleted successfully.",
    "regenerate_queued": "Image regeneration queued.",
    "preview_accepted": "Image accepted and saved successfully.",
    "preview_rejected": "Preview discarded.",
}


@router.get("", response_class=HTMLResponse)
async def list_images(
    request: Request,
    success: str | None = Query(default=None),
    style: str | None = Query(default=None),
    player_id: int | None = Query(default=None),
    draft_year: str | None = Query(default=None),
    q: str | None = Query(default=None),
    current_only: bool = Query(default=False),
    include_errors: bool = Query(default=False),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """List all images with filters and pagination (admin only)."""
    redirect, user = await require_admin(request, db)
    if redirect:
        return redirect

    # Convert draft_year from string to int
    draft_year_int: int | None = None
    if draft_year and draft_year.strip():
        try:
            draft_year_int = int(draft_year.strip())
        except ValueError:
            draft_year_int = None

    result = await svc_list_images(
        db,
        style=style,
        player_id=player_id,
        draft_year=draft_year_int,
        q=q,
        current_only=current_only,
        include_errors=include_errors,
        limit=limit,
        offset=offset,
    )

    # Calculate pagination info
    pages = (result.total + limit - 1) // limit if result.total > 0 else 1
    current_page = (offset // limit) + 1

    return request.app.state.templates.TemplateResponse(
        "admin/images/index.html",
        base_context(
            request,
            user=user,
            images=result.images,
            total=result.total,
            styles=result.styles,
            draft_years=result.draft_years,
            limit=limit,
            offset=offset,
            pages=pages,
            current_page=current_page,
            style=style,
            player_id=player_id,
            draft_year=draft_year_int,
            q=q,
            current_only=current_only,
            include_errors=include_errors,
            success=SUCCESS_MESSAGES.get(success) if success else None,
            active_nav="images",
        ),
    )


@router.get("/{asset_id}", response_class=HTMLResponse)
async def image_detail(
    request: Request,
    asset_id: int,
    success: str | None = Query(default=None),
    error: str | None = Query(default=None),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Display image detail page (admin only)."""
    redirect, user = await require_admin(request, db)
    if redirect:
        return redirect

    image = await get_image_by_id(db, asset_id)
    if image is None:
        raise HTTPException(status_code=404, detail="Image not found")

    return request.app.state.templates.TemplateResponse(
        "admin/images/detail.html",
        base_context(
            request,
            user=user,
            image=image,
            active_nav="images",
            success=SUCCESS_MESSAGES.get(success) if success else None,
            error=error,
        ),
    )


@router.post("/{asset_id}/delete", response_class=HTMLResponse)
async def delete_image(
    request: Request,
    asset_id: int,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Delete an image and redirect back to list (admin only)."""
    redirect, user = await require_admin(request, db)
    if redirect:
        return redirect

    async with db.begin():
        deleted = await svc_delete_image(db, asset_id)

    if not deleted:
        raise HTTPException(status_code=404, detail="Image not found")

    return RedirectResponse(url="/admin/images?success=deleted", status_code=303)


@router.post("/{asset_id}/regenerate", response_class=HTMLResponse)
async def regenerate_image(
    request: Request,
    asset_id: int,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Generate a preview for image regeneration (admin only).

    Generates a new image and redirects to the preview page for approval.
    """
    redirect, user = await require_admin(request, db)
    if redirect:
        return redirect

    # Get image and player info in one transaction block
    async with db.begin():
        image = await get_image_by_id(db, asset_id)
        if image is None:
            raise HTTPException(status_code=404, detail="Image not found")

        player = await get_player_by_id(db, image.player_id)
        if player is None:
            raise HTTPException(status_code=404, detail="Player not found")

    # Store player/image info needed for generation (outside transaction)
    player_id = image.player_id
    style = image.style
    source_asset_id = image.id

    try:
        # Generate preview image (no DB operations)
        preview_result = await image_generation_service.generate_preview(
            player=player,
            style=style,
            fetch_likeness=True,
        )

        # Store preview in database
        async with db.begin():
            preview = await svc_create_preview(
                db=db,
                player_id=player_id,
                source_asset_id=source_asset_id,
                style=style,
                preview_result=preview_result,
            )

        # Redirect to preview page
        return RedirectResponse(
            url=f"/admin/images/preview/{preview.id}",
            status_code=303,
        )

    except Exception as e:
        logger.exception(f"Failed to generate preview for asset {asset_id}")
        return RedirectResponse(
            url=f"/admin/images/{asset_id}?error=Generation+failed:+{str(e)[:100]}",
            status_code=303,
        )


@router.get("/preview/{preview_id}", response_class=HTMLResponse)
async def preview_image(
    request: Request,
    preview_id: int,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Display preview page with accept/reject/retry buttons (admin only)."""
    redirect, user = await require_admin(request, db)
    if redirect:
        return redirect

    preview = await get_preview_by_id(db, preview_id)
    if preview is None:
        raise HTTPException(status_code=404, detail="Preview not found or expired")

    return request.app.state.templates.TemplateResponse(
        "admin/images/preview.html",
        base_context(
            request,
            user=user,
            preview=preview,
            active_nav="images",
        ),
    )


@router.post("/preview/{preview_id}/accept", response_class=HTMLResponse)
async def accept_preview(
    request: Request,
    preview_id: int,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Accept preview: upload to S3 and create/update asset (admin only)."""
    redirect, user = await require_admin(request, db)
    if redirect:
        return redirect

    try:
        async with db.begin():
            asset = await svc_approve_preview(db, preview_id)

            if asset is None:
                raise HTTPException(
                    status_code=404, detail="Preview not found or expired"
                )

        # Redirect to the asset detail page
        return RedirectResponse(
            url=f"/admin/images/{asset.id}?success=preview_accepted",
            status_code=303,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to approve preview {preview_id}")
        # If approval fails, redirect back to preview with error
        return RedirectResponse(
            url=f"/admin/images/preview/{preview_id}?error=Failed+to+save:+{str(e)[:100]}",
            status_code=303,
        )


@router.post("/preview/{preview_id}/reject", response_class=HTMLResponse)
async def reject_preview(
    request: Request,
    preview_id: int,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Reject preview: delete it and redirect back to asset detail (admin only)."""
    redirect, user = await require_admin(request, db)
    if redirect:
        return redirect

    # Get preview to find source_asset_id for redirect, then delete
    async with db.begin():
        preview = await get_preview_by_id(db, preview_id)
        if preview is None:
            raise HTTPException(status_code=404, detail="Preview not found or expired")

        source_asset_id = preview.source_asset_id
        await svc_delete_preview(db, preview_id)

    # Redirect back to the original asset detail page
    if source_asset_id:
        return RedirectResponse(
            url=f"/admin/images/{source_asset_id}?success=preview_rejected",
            status_code=303,
        )
    else:
        return RedirectResponse(
            url="/admin/images?success=preview_rejected",
            status_code=303,
        )


@router.post("/preview/{preview_id}/retry", response_class=HTMLResponse)
async def retry_preview(
    request: Request,
    preview_id: int,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Retry preview: generate a new one and replace the current (admin only)."""
    redirect, user = await require_admin(request, db)
    if redirect:
        return redirect

    # Get current preview and player info
    async with db.begin():
        preview = await get_preview_by_id(db, preview_id)
        if preview is None:
            raise HTTPException(status_code=404, detail="Preview not found or expired")

        player = await get_player_by_id(db, preview.player_id)
        if player is None:
            raise HTTPException(status_code=404, detail="Player not found")

    # Store info needed for generation (outside transaction)
    player_id = preview.player_id
    source_asset_id = preview.source_asset_id
    style = preview.style

    try:
        # Generate new preview image (no DB operations)
        preview_result = await image_generation_service.generate_preview(
            player=player,
            style=style,
            fetch_likeness=True,
        )

        # Delete old preview and create new one
        async with db.begin():
            await svc_delete_preview(db, preview_id)
            new_preview = await svc_create_preview(
                db=db,
                player_id=player_id,
                source_asset_id=source_asset_id,
                style=style,
                preview_result=preview_result,
            )

        # Redirect to new preview page
        return RedirectResponse(
            url=f"/admin/images/preview/{new_preview.id}",
            status_code=303,
        )

    except Exception as e:
        logger.exception(f"Failed to regenerate preview for player {player_id}")
        return RedirectResponse(
            url=f"/admin/images/preview/{preview_id}?error=Generation+failed:+{str(e)[:100]}",
            status_code=303,
        )
