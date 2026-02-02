"""Admin PlayerMaster CRUD routes.

Provides list, create, read, update, and delete for player records.
Routes are thin wrappers; business logic lives in admin_player_service.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app.routes.admin.helpers import base_context, require_admin
from app.schemas.auth import AuthUser
from app.schemas.player_status import PlayerStatus
from app.schemas.players_master import PlayerMaster
from app.utils.images import (
    get_placeholder_url,
    get_player_image_url,
    get_s3_image_base_url,
)
from app.services.admin_player_service import (
    PlayerFormData,
    PlayerListResult,
    PlayerStatusFormData,
    can_delete_player,
    create_player as svc_create_player,
    delete_player as svc_delete_player,
    get_player_by_id,
    get_player_status_by_player_id,
    list_players as svc_list_players,
    parse_player_form,
    update_player as svc_update_player,
    update_player_status as svc_update_player_status,
    validate_player_form,
)
from app.utils.db_async import get_session

router = APIRouter(prefix="/players", tags=["admin-players"])

# Default pagination values
DEFAULT_LIMIT = 25
MAX_LIMIT = 100

# Success messages for flash-style notifications
SUCCESS_MESSAGES = {
    "created": "Player created successfully.",
    "updated": "Player updated successfully.",
    "deleted": "Player deleted successfully.",
}


def _render_form_error(
    request: Request,
    user: AuthUser | None,
    player: PlayerMaster | None,
    error: str,
    player_status: PlayerStatus | None = None,
) -> Response:
    """Render create/edit form with an error message."""
    template = "admin/players/detail.html" if player else "admin/players/form.html"
    return request.app.state.templates.TemplateResponse(
        template,
        base_context(
            request,
            user=user,
            player=player,
            player_status=player_status,
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
    draft_year: str | None = Query(default=None),
    position: str | None = Query(default=None),
    nba_status: str | None = Query(default=None),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """List all players with pagination and filters (admin only)."""
    redirect, user = await require_admin(request, db)
    if redirect:
        return redirect

    # Convert draft_year from string to int (empty string becomes None)
    draft_year_int: int | None = None
    if draft_year and draft_year.strip():
        try:
            draft_year_int = int(draft_year.strip())
        except ValueError:
            draft_year_int = None

    result = await svc_list_players(
        db, q, draft_year_int, position, nba_status, limit, offset
    )

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
            draft_year=draft_year_int,
            position=position,
            nba_status=nba_status,
            draft_years=result.draft_years,
            success=SUCCESS_MESSAGES.get(success) if success else None,
            active_nav="players",
        ),
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
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Display the edit player form (admin only)."""
    redirect, user = await require_admin(request, db)
    if redirect:
        return redirect

    player = await get_player_by_id(db, player_id)
    if player is None:
        raise HTTPException(status_code=404, detail="Player not found")

    # Fetch player status data
    player_status = await get_player_status_by_player_id(db, player_id)

    # Build S3-first image URL (source of truth for display)
    expected_image_url = None
    if player.slug:
        expected_image_url = get_player_image_url(
            player_id=player_id,
            slug=player.slug,
            style="default",
            base_url=get_s3_image_base_url(),
        )

    # Build placeholder URL for onerror fallback
    placeholder_url = get_placeholder_url(player.display_name, player_id=player_id)

    return request.app.state.templates.TemplateResponse(
        "admin/players/detail.html",
        base_context(
            request,
            user=user,
            player=player,
            player_status=player_status,
            expected_image_url=expected_image_url,
            placeholder_url=placeholder_url,
            error=None,
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
    # Player status fields
    is_active_nba: str | None = Form(default=None),
    current_team: str | None = Form(default=None),
    nba_last_season: str | None = Form(default=None),
    raw_position: str | None = Form(default=None),
    height_in: str | None = Form(default=None),
    weight_lb: str | None = Form(default=None),
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

        # Fetch player status for error re-renders
        player_status = await get_player_status_by_player_id(db, player_id)

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
            return _render_form_error(request, user, player, error, player_status)

        # Parse form data to typed values
        parsed = parse_player_form(form_data)
        if isinstance(parsed, str):
            return _render_form_error(request, user, player, parsed, player_status)

        await svc_update_player(db, player, parsed)

        # Update player status
        status_data = PlayerStatusFormData(
            is_active_nba=is_active_nba,
            current_team=current_team,
            nba_last_season=nba_last_season,
            raw_position=raw_position,
            height_in=height_in,
            weight_lb=weight_lb,
        )
        await svc_update_player_status(db, player_id, status_data)
    return RedirectResponse(url="/admin/players?success=updated", status_code=303)


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
        can_delete, error_reason = await can_delete_player(db, player_id)
        if not can_delete:
            # Re-fetch list data for rendering
            list_result = await svc_list_players(
                db, None, None, None, None, DEFAULT_LIMIT, 0
            )
            return _render_list_error(
                request,
                user,
                list_result,
                f"Cannot delete '{player.display_name}': {error_reason}",
            )

        await svc_delete_player(db, player)
    return RedirectResponse(url="/admin/players?success=deleted", status_code=303)
