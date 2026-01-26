"""Admin PlayerMaster CRUD routes.

Provides list, create, read, update, and delete for player records.
Routes are thin wrappers; business logic lives in admin_player_service.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app.routes.admin.helpers import base_context, require_admin
from app.schemas.auth import AuthUser
from app.schemas.players_master import PlayerMaster
from app.services.admin_player_related_service import (
    count_player_aliases,
    count_player_external_ids,
    get_player_status,
)
from app.services.admin_player_service import (
    ImageValidationResult,
    PlayerDependencies,
    PlayerFormData,
    PlayerListResult,
    can_delete_player,
    create_image_snapshot,
    create_player as svc_create_player,
    delete_player as svc_delete_player,
    get_latest_image_asset,
    get_player_by_id,
    get_player_dependencies,
    list_players as svc_list_players,
    parse_player_form,
    update_player as svc_update_player,
    update_snapshot_counts,
    validate_image_url,
    validate_player_form,
)
from app.services.image_generation import image_generation_service
from app.utils.db_async import get_session

router = APIRouter(prefix="/players", tags=["admin-players"])

# Default pagination values
DEFAULT_LIMIT = 25
MAX_LIMIT = 100

# Image generation styles
IMAGE_STYLES = ["default", "vector", "comic", "retro"]

# Success messages for flash-style notifications
SUCCESS_MESSAGES = {
    "created": "Player created successfully.",
    "updated": "Player updated successfully.",
    "deleted": "Player deleted successfully.",
    "image_generated": "Image generated successfully.",
    "status_deleted": "Status deleted successfully.",
}


def _render_form_error(
    request: Request,
    user: AuthUser | None,
    player: PlayerMaster | None,
    error: str,
) -> Response:
    """Render create/edit form with an error message."""
    template = "admin/players/detail.html" if player else "admin/players/form.html"
    return request.app.state.templates.TemplateResponse(
        template,
        base_context(
            request,
            user=user,
            player=player,
            error=error,
            active_nav="players",
        ),
    )


def _render_list_error(
    request: Request,
    user: AuthUser | None,
    list_result: PlayerListResult,
    error: str,
) -> Response:
    """Render player list with an error message."""
    pages = (
        (list_result.total + DEFAULT_LIMIT - 1) // DEFAULT_LIMIT
        if list_result.total > 0
        else 1
    )
    return request.app.state.templates.TemplateResponse(
        "admin/players/index.html",
        base_context(
            request,
            user=user,
            players=list_result.players,
            total=list_result.total,
            limit=DEFAULT_LIMIT,
            offset=0,
            pages=pages,
            current_page=1,
            q=None,
            draft_year=None,
            position=None,
            draft_years=list_result.draft_years,
            error=error,
            success=None,
            active_nav="players",
        ),
    )


def _build_form_data(
    display_name: str,
    first_name: str,
    last_name: str,
    prefix: str | None,
    middle_name: str | None,
    suffix: str | None,
    birthdate: str | None,
    birth_city: str | None,
    birth_state_province: str | None,
    birth_country: str | None,
    school: str | None,
    high_school: str | None,
    shoots: str | None,
    draft_year: str | None,
    draft_round: str | None,
    draft_pick: str | None,
    draft_team: str | None,
    nba_debut_date: str | None,
    nba_debut_season: str | None,
    reference_image_url: str | None,
) -> PlayerFormData:
    """Build PlayerFormData from individual form fields."""
    return PlayerFormData(
        display_name=display_name,
        first_name=first_name,
        last_name=last_name,
        prefix=prefix,
        middle_name=middle_name,
        suffix=suffix,
        birthdate=birthdate,
        birth_city=birth_city,
        birth_state_province=birth_state_province,
        birth_country=birth_country,
        school=school,
        high_school=high_school,
        shoots=shoots,
        draft_year=draft_year,
        draft_round=draft_round,
        draft_pick=draft_pick,
        draft_team=draft_team,
        nba_debut_date=nba_debut_date,
        nba_debut_season=nba_debut_season,
        reference_image_url=reference_image_url,
    )


@router.get("", response_class=HTMLResponse)
async def list_players(
    request: Request,
    success: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    q: str | None = Query(default=None),
    draft_year: int | None = Query(default=None),
    position: str | None = Query(default=None),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """List all players with pagination and filters (admin only)."""
    redirect, user = await require_admin(request, db)
    if redirect:
        return redirect

    result = await svc_list_players(db, q, draft_year, position, limit, offset)

    # Calculate pagination info
    pages = (result.total + limit - 1) // limit if result.total > 0 else 1
    current_page = (offset // limit) + 1

    return request.app.state.templates.TemplateResponse(
        "admin/players/index.html",
        base_context(
            request,
            user=user,
            players=result.players,
            total=result.total,
            limit=limit,
            offset=offset,
            pages=pages,
            current_page=current_page,
            q=q,
            draft_year=draft_year,
            position=position,
            draft_years=result.draft_years,
            success=SUCCESS_MESSAGES.get(success) if success else None,
            active_nav="players",
        ),
    )


@router.post("/validate-image-url")
async def validate_image_url_endpoint(
    request: Request,
    url: str = Form(...),
    db: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Validate an image URL (admin only).

    Returns JSON with validation result.
    """
    redirect, _ = await require_admin(request, db)
    if redirect:
        return JSONResponse(
            status_code=401,
            content={"valid": False, "error": "Unauthorized"},
        )

    result: ImageValidationResult = await validate_image_url(url)
    return JSONResponse(
        content={
            "valid": result.valid,
            "content_type": result.content_type,
            "error": result.error,
        }
    )


@router.get("/new", response_class=HTMLResponse)
async def new_player(
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Display the create player form (admin only)."""
    redirect, user = await require_admin(request, db)
    if redirect:
        return redirect

    return request.app.state.templates.TemplateResponse(
        "admin/players/form.html",
        base_context(
            request,
            user=user,
            player=None,
            error=None,
            active_nav="players",
        ),
    )


@router.post("", response_class=HTMLResponse)
async def create_player(
    request: Request,
    display_name: str = Form(...),
    first_name: str = Form(...),
    last_name: str = Form(...),
    prefix: str | None = Form(default=None),
    middle_name: str | None = Form(default=None),
    suffix: str | None = Form(default=None),
    birthdate: str | None = Form(default=None),
    birth_city: str | None = Form(default=None),
    birth_state_province: str | None = Form(default=None),
    birth_country: str | None = Form(default=None),
    school: str | None = Form(default=None),
    high_school: str | None = Form(default=None),
    shoots: str | None = Form(default=None),
    draft_year: str | None = Form(default=None),
    draft_round: str | None = Form(default=None),
    draft_pick: str | None = Form(default=None),
    draft_team: str | None = Form(default=None),
    nba_debut_date: str | None = Form(default=None),
    nba_debut_season: str | None = Form(default=None),
    reference_image_url: str | None = Form(default=None),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Create a new player (admin only)."""
    redirect, user = await require_admin(request, db)
    if redirect:
        return redirect

    form_data = _build_form_data(
        display_name,
        first_name,
        last_name,
        prefix,
        middle_name,
        suffix,
        birthdate,
        birth_city,
        birth_state_province,
        birth_country,
        school,
        high_school,
        shoots,
        draft_year,
        draft_round,
        draft_pick,
        draft_team,
        nba_debut_date,
        nba_debut_season,
        reference_image_url,
    )

    # Validate required fields
    if error := validate_player_form(form_data):
        return _render_form_error(request, user, None, error)

    # Parse form data to typed values
    parsed = parse_player_form(form_data)
    if isinstance(parsed, str):
        return _render_form_error(request, user, None, parsed)

    async with db.begin():
        await svc_create_player(db, parsed)
    return RedirectResponse(url="/admin/players?success=created", status_code=303)


@router.get("/{player_id}", response_class=HTMLResponse)
async def edit_player(
    request: Request,
    player_id: int,
    success: str | None = Query(default=None),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Display the edit player form (admin only)."""
    redirect, user = await require_admin(request, db)
    if redirect:
        return redirect

    player = await get_player_by_id(db, player_id)
    if player is None:
        raise HTTPException(status_code=404, detail="Player not found")

    latest_image = await get_latest_image_asset(db, player_id)

    # Get related data counts for the Related Data section
    status = await get_player_status(db, player_id)
    alias_count = await count_player_aliases(db, player_id)
    external_id_count = await count_player_external_ids(db, player_id)

    return request.app.state.templates.TemplateResponse(
        "admin/players/detail.html",
        base_context(
            request,
            user=user,
            player=player,
            error=None,
            success=SUCCESS_MESSAGES.get(success) if success else None,
            latest_image=latest_image,
            image_styles=IMAGE_STYLES,
            has_status=status is not None,
            alias_count=alias_count,
            external_id_count=external_id_count,
            active_nav="players",
        ),
    )


@router.post("/{player_id}", response_class=HTMLResponse)
async def update_player(
    request: Request,
    player_id: int,
    display_name: str = Form(...),
    first_name: str = Form(...),
    last_name: str = Form(...),
    prefix: str | None = Form(default=None),
    middle_name: str | None = Form(default=None),
    suffix: str | None = Form(default=None),
    birthdate: str | None = Form(default=None),
    birth_city: str | None = Form(default=None),
    birth_state_province: str | None = Form(default=None),
    birth_country: str | None = Form(default=None),
    school: str | None = Form(default=None),
    high_school: str | None = Form(default=None),
    shoots: str | None = Form(default=None),
    draft_year: str | None = Form(default=None),
    draft_round: str | None = Form(default=None),
    draft_pick: str | None = Form(default=None),
    draft_team: str | None = Form(default=None),
    nba_debut_date: str | None = Form(default=None),
    nba_debut_season: str | None = Form(default=None),
    reference_image_url: str | None = Form(default=None),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Update a player (admin only)."""
    redirect, user = await require_admin(request, db)
    if redirect:
        return redirect

    async with db.begin():
        player = await get_player_by_id(db, player_id)
        if player is None:
            raise HTTPException(status_code=404, detail="Player not found")

        form_data = _build_form_data(
            display_name,
            first_name,
            last_name,
            prefix,
            middle_name,
            suffix,
            birthdate,
            birth_city,
            birth_state_province,
            birth_country,
            school,
            high_school,
            shoots,
            draft_year,
            draft_round,
            draft_pick,
            draft_team,
            nba_debut_date,
            nba_debut_season,
            reference_image_url,
        )

        # Validate required fields
        if error := validate_player_form(form_data):
            return _render_form_error(request, user, player, error)

        # Parse form data to typed values
        parsed = parse_player_form(form_data)
        if isinstance(parsed, str):
            return _render_form_error(request, user, player, parsed)

        await svc_update_player(db, player, parsed)
    return RedirectResponse(url="/admin/players?success=updated", status_code=303)


@router.post("/{player_id}/generate-image", response_class=HTMLResponse)
async def generate_image(
    request: Request,
    player_id: int,
    style: str = Form(default="default"),
    use_likeness: bool = Form(default=False),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Generate an AI image for a player (admin only).

    Requires GEMINI_API_KEY to be configured.
    """
    redirect, user = await require_admin(request, db)
    if redirect:
        return redirect

    player = await get_player_by_id(db, player_id)
    if player is None:
        raise HTTPException(status_code=404, detail="Player not found")

    # Validate style
    if style not in IMAGE_STYLES:
        style = "default"

    # Check for reference image if likeness requested
    if use_likeness and not player.reference_image_url:
        return request.app.state.templates.TemplateResponse(
            "admin/players/detail.html",
            base_context(
                request,
                user=user,
                player=player,
                error="Cannot use likeness: No reference image URL set.",
                latest_image=await get_latest_image_asset(db, player_id),
                image_styles=IMAGE_STYLES,
                active_nav="players",
            ),
        )

    try:
        # Get system prompt
        system_prompt = image_generation_service.get_system_prompt("default")

        async with db.begin():
            # Create snapshot for this generation
            snapshot = await create_image_snapshot(
                db=db,
                player=player,
                style=style,
                system_prompt=system_prompt,
                system_prompt_version="default",
                image_size="1K",
            )

            # Generate the image
            asset = await image_generation_service.generate_for_player(
                db=db,
                player=player,
                snapshot=snapshot,
                style=style,
                fetch_likeness=use_likeness,
            )

            # Update snapshot counts
            await update_snapshot_counts(
                db, snapshot, success=asset.error_message is None
            )

        if asset.error_message:
            return request.app.state.templates.TemplateResponse(
                "admin/players/detail.html",
                base_context(
                    request,
                    user=user,
                    player=player,
                    error=f"Image generation failed: {asset.error_message}",
                    latest_image=asset,
                    image_styles=IMAGE_STYLES,
                    active_nav="players",
                ),
            )

        return RedirectResponse(
            url=f"/admin/players/{player_id}?success=image_generated",
            status_code=303,
        )

    except ValueError as e:
        # Gemini API key not configured
        return request.app.state.templates.TemplateResponse(
            "admin/players/detail.html",
            base_context(
                request,
                user=user,
                player=player,
                error=f"Configuration error: {e}",
                latest_image=await get_latest_image_asset(db, player_id),
                image_styles=IMAGE_STYLES,
                active_nav="players",
            ),
        )
    except Exception as e:
        return request.app.state.templates.TemplateResponse(
            "admin/players/detail.html",
            base_context(
                request,
                user=user,
                player=player,
                error=f"Unexpected error: {e}",
                latest_image=await get_latest_image_asset(db, player_id),
                image_styles=IMAGE_STYLES,
                active_nav="players",
            ),
        )


def _format_dependencies_error(deps: PlayerDependencies) -> str:
    """Format a human-readable error message from dependencies."""
    parts = []
    if deps.player_status:
        parts.append(f"{deps.player_status} status record(s)")
    if deps.player_aliases:
        parts.append(f"{deps.player_aliases} alias(es)")
    if deps.player_external_ids:
        parts.append(f"{deps.player_external_ids} external ID(s)")
    if deps.player_bio_snapshots:
        parts.append(f"{deps.player_bio_snapshots} bio snapshot(s)")
    if deps.combine_agility:
        parts.append(f"{deps.combine_agility} agility record(s)")
    if deps.combine_anthro:
        parts.append(f"{deps.combine_anthro} anthro record(s)")
    if deps.combine_shooting:
        parts.append(f"{deps.combine_shooting} shooting record(s)")
    if deps.player_metric_values:
        parts.append(f"{deps.player_metric_values} metric value(s)")
    if deps.player_similarity_anchor + deps.player_similarity_comparison:
        sim_count = deps.player_similarity_anchor + deps.player_similarity_comparison
        parts.append(f"{sim_count} similarity record(s)")
    if deps.news_items:
        parts.append(f"{deps.news_items} news item(s)")
    if deps.image_assets:
        parts.append(f"{deps.image_assets} image asset(s)")

    return "it has " + ", ".join(parts) + "."


@router.get("/{player_id}/delete", response_class=HTMLResponse)
async def confirm_delete_player(
    request: Request,
    player_id: int,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Display the delete confirmation page (admin only)."""
    redirect, user = await require_admin(request, db)
    if redirect:
        return redirect

    player = await get_player_by_id(db, player_id)
    if player is None:
        raise HTTPException(status_code=404, detail="Player not found")

    deps = await get_player_dependencies(db, player_id)

    return request.app.state.templates.TemplateResponse(
        "admin/players/delete.html",
        base_context(
            request,
            user=user,
            player=player,
            deps=deps,
            can_delete=not deps.has_any,
            active_nav="players",
        ),
    )


@router.post("/{player_id}/delete", response_class=HTMLResponse)
async def delete_player(
    request: Request,
    player_id: int,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Delete a player (admin only)."""
    redirect, user = await require_admin(request, db)
    if redirect:
        return redirect

    async with db.begin():
        player = await get_player_by_id(db, player_id)
        if player is None:
            raise HTTPException(status_code=404, detail="Player not found")

        # Check for dependencies
        can_delete, deps = await can_delete_player(db, player_id)
        if not can_delete:
            # Re-fetch list data for rendering
            list_result = await svc_list_players(db, None, None, None, DEFAULT_LIMIT, 0)
            return _render_list_error(
                request,
                user,
                list_result,
                f"Cannot delete '{player.display_name}': {_format_dependencies_error(deps)}",
            )

        await svc_delete_player(db, player)
    return RedirectResponse(url="/admin/players?success=deleted", status_code=303)
