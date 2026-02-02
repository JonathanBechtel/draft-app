"""Admin user management routes."""

from __future__ import annotations

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Form,
    HTTPException,
    Query,
    Request,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app.routes.admin.helpers import base_context, require_admin
from app.services.email_worker import send_pending_emails
from app.services.admin_auth_service import (
    create_invited_user,
    delete_user,
    get_user_by_id,
    list_users,
    resend_invite,
    update_user,
)
from app.services.admin_permission_service import (
    KNOWN_DATASETS,
    DatasetPermission,
    get_user_permissions,
    set_user_permissions,
)
from app.utils.db_async import get_session

router = APIRouter(prefix="/users", tags=["admin-users"])

VALID_ROLES = ["admin", "worker"]


@router.get("", response_class=HTMLResponse)
async def list_all_users(
    request: Request,
    success: str | None = Query(default=None),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """List all users (admin only)."""
    redirect, user = await require_admin(request, db, next_path="/admin/users")
    if redirect:
        return redirect

    users = await list_users(db)

    success_messages = {
        "created": "User invitation sent successfully.",
        "updated": "User updated successfully.",
        "deleted": "User deleted successfully.",
        "resent": "Invitation resent successfully.",
        "permissions": "Permissions updated successfully.",
    }

    return request.app.state.templates.TemplateResponse(
        "admin/users/index.html",
        base_context(
            request,
            user=user,
            users=users,
            success=success_messages.get(success) if success else None,
        ),
    )


@router.get("/new", response_class=HTMLResponse)
async def new_user(
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Display the invite user form (admin only)."""
    redirect, user = await require_admin(request, db, next_path="/admin/users/new")
    if redirect:
        return redirect

    return request.app.state.templates.TemplateResponse(
        "admin/users/form.html",
        base_context(
            request,
            user=user,
            edit_user=None,
            roles=VALID_ROLES,
            error=None,
        ),
    )


@router.post("", response_class=HTMLResponse)
async def create_user(
    request: Request,
    background_tasks: BackgroundTasks,
    email: str = Form(...),
    role: str = Form(...),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Create and invite a new user (admin only)."""
    redirect, user = await require_admin(request, db, next_path="/admin/users/new")
    if redirect:
        return redirect

    # Validate role
    if role not in VALID_ROLES:
        return request.app.state.templates.TemplateResponse(
            "admin/users/form.html",
            base_context(
                request,
                user=user,
                edit_user=None,
                roles=VALID_ROLES,
                error=f"Invalid role: {role}",
            ),
        )

    new_user_obj, _raw_token, error = await create_invited_user(
        db, email=email, role=role
    )

    if error:
        return request.app.state.templates.TemplateResponse(
            "admin/users/form.html",
            base_context(
                request,
                user=user,
                edit_user=None,
                roles=VALID_ROLES,
                error=error,
            ),
        )

    # Send invitation email in background
    background_tasks.add_task(send_pending_emails, batch_size=1)

    return RedirectResponse(url="/admin/users?success=created", status_code=303)


@router.get("/{user_id}", response_class=HTMLResponse)
async def edit_user_form(
    request: Request,
    user_id: int,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Display the edit user form (admin only)."""
    redirect, user = await require_admin(request, db, next_path="/admin/users")
    if redirect:
        return redirect

    edit_user = await get_user_by_id(db, user_id=user_id)
    if edit_user is None:
        raise HTTPException(status_code=404, detail="User not found")

    return request.app.state.templates.TemplateResponse(
        "admin/users/form.html",
        base_context(
            request,
            user=user,
            edit_user=edit_user,
            roles=VALID_ROLES,
            error=None,
        ),
    )


@router.post("/{user_id}", response_class=HTMLResponse)
async def update_user_route(
    request: Request,
    user_id: int,
    role: str = Form(...),
    is_active: str | None = Form(default=None),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Update a user (admin only)."""
    redirect, user = await require_admin(request, db, next_path="/admin/users")
    if redirect:
        return redirect

    # Validate role
    if role not in VALID_ROLES:
        edit_user = await get_user_by_id(db, user_id=user_id)
        if edit_user is None:
            raise HTTPException(status_code=404, detail="User not found")

        return request.app.state.templates.TemplateResponse(
            "admin/users/form.html",
            base_context(
                request,
                user=user,
                edit_user=edit_user,
                roles=VALID_ROLES,
                error=f"Invalid role: {role}",
            ),
        )

    active = is_active is not None and is_active not in {"0", "", "false", "False"}

    updated_user = await update_user(
        db,
        user_id=user_id,
        role=role,
        is_active=active,
    )

    if updated_user is None:
        raise HTTPException(status_code=404, detail="User not found")

    return RedirectResponse(url="/admin/users?success=updated", status_code=303)


@router.post("/{user_id}/resend-invite", response_class=HTMLResponse)
async def resend_invite_route(
    request: Request,
    background_tasks: BackgroundTasks,
    user_id: int,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Resend invitation to a user (admin only)."""
    redirect, user = await require_admin(request, db, next_path="/admin/users")
    if redirect:
        return redirect

    _raw_token, error = await resend_invite(db, user_id=user_id)

    if error:
        users = await list_users(db)
        return request.app.state.templates.TemplateResponse(
            "admin/users/index.html",
            base_context(
                request,
                user=user,
                users=users,
                error=error,
            ),
        )

    # Send invitation email in background
    background_tasks.add_task(send_pending_emails, batch_size=1)

    return RedirectResponse(url="/admin/users?success=resent", status_code=303)


@router.post("/{user_id}/delete", response_class=HTMLResponse)
async def delete_user_route(
    request: Request,
    user_id: int,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Delete a user (admin only)."""
    redirect, user = await require_admin(request, db, next_path="/admin/users")
    if redirect:
        return redirect

    if user is None or user.id is None:
        return RedirectResponse(url="/admin/login", status_code=303)

    success, error = await delete_user(db, user_id=user_id, current_user_id=user.id)

    if error:
        users = await list_users(db)
        return request.app.state.templates.TemplateResponse(
            "admin/users/index.html",
            base_context(
                request,
                user=user,
                users=users,
                error=error,
            ),
        )

    return RedirectResponse(url="/admin/users?success=deleted", status_code=303)


@router.get("/{user_id}/permissions", response_class=HTMLResponse)
async def edit_permissions_form(
    request: Request,
    user_id: int,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Display the permission editor (admin only)."""
    redirect, user = await require_admin(request, db, next_path="/admin/users")
    if redirect:
        return redirect

    edit_user = await get_user_by_id(db, user_id=user_id)
    if edit_user is None:
        raise HTTPException(status_code=404, detail="User not found")

    permissions = await get_user_permissions(db, user_id=user_id)

    return request.app.state.templates.TemplateResponse(
        "admin/users/permissions.html",
        base_context(
            request,
            user=user,
            edit_user=edit_user,
            permissions=permissions,
            datasets=KNOWN_DATASETS,
        ),
    )


@router.post("/{user_id}/permissions", response_class=HTMLResponse)
async def update_permissions(
    request: Request,
    user_id: int,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Update user permissions (admin only)."""
    redirect, user = await require_admin(request, db, next_path="/admin/users")
    if redirect:
        return redirect

    edit_user = await get_user_by_id(db, user_id=user_id)
    if edit_user is None:
        raise HTTPException(status_code=404, detail="User not found")

    # Parse form data for permissions
    form_data = await request.form()
    permissions = []

    for dataset in KNOWN_DATASETS:
        can_view = form_data.get(f"{dataset}_view") is not None
        can_edit = form_data.get(f"{dataset}_edit") is not None
        permissions.append(
            DatasetPermission(
                dataset=dataset,
                can_view=can_view,
                can_edit=can_edit,
            )
        )

    await set_user_permissions(db, user_id=user_id, permissions=permissions)

    return RedirectResponse(url="/admin/users?success=permissions", status_code=303)
