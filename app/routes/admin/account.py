"""Admin account management routes."""

from __future__ import annotations

import hashlib
import hmac

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app.config import settings
from app.routes.admin.helpers import base_context, require_auth
from app.services.admin_auth_service import change_password
from app.utils.db_async import get_session

router = APIRouter(prefix="/account", tags=["admin-account"])


@router.get("", response_class=HTMLResponse)
async def admin_account(
    request: Request,
    success: str | None = Query(default=None),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Display the current user's account info."""
    redirect, user = await require_auth(request, db, next_path="/admin/account")
    if redirect:
        return redirect

    success_message = None
    if success == "password_changed":
        success_message = "Your password has been changed successfully."

    return request.app.state.templates.TemplateResponse(
        "admin/account.html",
        base_context(request, user=user, success=success_message),
    )


@router.get("/change-password", response_class=HTMLResponse)
async def admin_change_password_form(
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Display the change password form."""
    redirect, user = await require_auth(
        request, db, next_path="/admin/account/change-password"
    )
    if redirect:
        return redirect

    return request.app.state.templates.TemplateResponse(
        "admin/change-password.html",
        base_context(request, user=user, error=None),
    )


@router.post("/change-password", response_class=HTMLResponse)
async def admin_change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Process the change password form."""
    redirect, user = await require_auth(
        request, db, next_path="/admin/account/change-password"
    )
    if redirect:
        return redirect

    if new_password != confirm_password:
        return request.app.state.templates.TemplateResponse(
            "admin/change-password.html",
            base_context(
                request,
                user=user,
                error="New password and confirmation do not match.",
            ),
        )

    if user is None or user.id is None:
        return request.app.state.templates.TemplateResponse(
            "admin/change-password.html",
            base_context(request, user=user, error="Unable to change password."),
        )

    # Hash the current session token to preserve it
    from app.services.admin_auth_service import ADMIN_SESSION_COOKIE_NAME

    raw_token = request.cookies.get(ADMIN_SESSION_COOKIE_NAME)
    current_token_hash = None
    if raw_token:
        key = settings.secret_key.encode("utf-8")
        current_token_hash = hmac.new(
            key, raw_token.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    success, error_msg = await change_password(
        db,
        user_id=user.id,
        current_password=current_password,
        new_password=new_password,
        current_session_token_hash=current_token_hash,
    )

    if not success:
        return request.app.state.templates.TemplateResponse(
            "admin/change-password.html",
            base_context(request, user=user, error=error_msg),
        )

    return RedirectResponse(
        url="/admin/account?success=password_changed", status_code=303
    )
