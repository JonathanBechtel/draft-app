"""Admin authentication routes (login, logout, password reset)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app.config import settings
from app.routes.admin.helpers import base_context
from app.services.admin_auth_service import (
    ADMIN_SESSION_COOKIE_NAME,
    REMEMBER_ME_TTL,
    authenticate_staff_user,
    confirm_invitation,
    confirm_password_reset,
    enqueue_password_reset,
    get_invite_token_user,
    issue_session,
    revoke_session,
    sanitize_next_path,
)
from app.utils.db_async import get_session

router = APIRouter(tags=["admin-auth"])


@router.get("/login", response_class=HTMLResponse)
async def admin_login_page(
    request: Request,
    next: str | None = Query(default=None),
) -> Response:
    """Render the login form."""
    return request.app.state.templates.TemplateResponse(
        "admin/login.html",
        base_context(request, next=sanitize_next_path(next), error=None),
    )


@router.post("/login", response_class=HTMLResponse)
async def admin_login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    remember: str | None = Form(default=None),
    next: str | None = Query(default=None),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Handle staff login and set a session cookie on success."""
    user = await authenticate_staff_user(db, email=email, password=password)
    if user is None or user.id is None:
        return request.app.state.templates.TemplateResponse(
            "admin/login.html",
            base_context(
                request,
                next=sanitize_next_path(next),
                error="Invalid email or password.",
            ),
            status_code=200,
        )

    remember_me = remember is not None and remember not in {"0", "", "false", "False"}
    raw_token, _session = await issue_session(
        db,
        user_id=user.id,
        remember_me=remember_me,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )

    redirect_target = sanitize_next_path(next)
    response = RedirectResponse(url=redirect_target, status_code=303)

    response.set_cookie(
        key=ADMIN_SESSION_COOKIE_NAME,
        value=raw_token,
        httponly=True,
        samesite="lax",
        secure=not settings.is_dev,
        path="/",
        max_age=int(REMEMBER_ME_TTL.total_seconds()) if remember_me else None,
    )
    return response


@router.post("/logout")
async def admin_logout(
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Revoke the current session and clear the cookie."""
    raw_token = request.cookies.get(ADMIN_SESSION_COOKIE_NAME)
    if raw_token:
        await revoke_session(db, raw_token=raw_token)

    response = RedirectResponse(url="/admin/login", status_code=303)
    response.delete_cookie(ADMIN_SESSION_COOKIE_NAME, path="/")
    return response


# ---------------------------------------------------------------------------
# Password Reset (unauthenticated)
# ---------------------------------------------------------------------------


@router.get("/password-reset", response_class=HTMLResponse)
async def admin_password_reset_form(request: Request) -> Response:
    """Display the password reset request form."""
    return request.app.state.templates.TemplateResponse(
        "admin/password-reset-request.html",
        base_context(request),
    )


@router.post("/password-reset", response_class=HTMLResponse)
async def admin_password_reset(
    request: Request,
    email: str = Form(...),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Request a password reset (generic response to avoid user enumeration)."""
    await enqueue_password_reset(db, email=email)
    return request.app.state.templates.TemplateResponse(
        "admin/password-reset-request.html",
        base_context(request, submitted=True),
    )


@router.get("/password-reset/confirm", response_class=HTMLResponse)
async def admin_password_reset_confirm_form(
    request: Request,
    token: str | None = Query(default=None),
) -> Response:
    """Display the password reset confirmation form."""
    if not token:
        return RedirectResponse(url="/admin/password-reset", status_code=303)

    return request.app.state.templates.TemplateResponse(
        "admin/password-reset-confirm.html",
        base_context(request, token=token, error=None),
    )


@router.post("/password-reset/confirm", response_class=HTMLResponse)
async def admin_password_reset_confirm(
    request: Request,
    token: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Consume a reset token and set a new password."""
    if password != confirm_password:
        return request.app.state.templates.TemplateResponse(
            "admin/password-reset-confirm.html",
            base_context(request, token=token, error="Passwords do not match."),
        )

    if len(password) < 8:
        return request.app.state.templates.TemplateResponse(
            "admin/password-reset-confirm.html",
            base_context(
                request, token=token, error="Password must be at least 8 characters."
            ),
        )

    ok = await confirm_password_reset(db, raw_token=token, new_password=password)
    if not ok:
        return request.app.state.templates.TemplateResponse(
            "admin/password-reset-confirm.html",
            base_context(
                request,
                token=token,
                error="Reset token is invalid or expired. Please request a new one.",
            ),
        )

    response = RedirectResponse(url="/admin/password-reset/success", status_code=303)
    response.delete_cookie(ADMIN_SESSION_COOKIE_NAME, path="/")
    return response


@router.get("/password-reset/success", response_class=HTMLResponse)
async def admin_password_reset_success(request: Request) -> Response:
    """Display the password reset success page."""
    return request.app.state.templates.TemplateResponse(
        "admin/password-reset-success.html",
        base_context(request),
    )


# ---------------------------------------------------------------------------
# Invitation Acceptance (unauthenticated)
# ---------------------------------------------------------------------------


@router.get("/invite/accept", response_class=HTMLResponse)
async def admin_invite_accept_form(
    request: Request,
    token: str | None = Query(default=None),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Display the invitation acceptance form."""
    if not token:
        return request.app.state.templates.TemplateResponse(
            "admin/invite-accept.html",
            base_context(
                request,
                token=None,
                invited_user=None,
                error="Invalid invitation link.",
            ),
        )

    invited_user = await get_invite_token_user(db, raw_token=token)
    if invited_user is None:
        return request.app.state.templates.TemplateResponse(
            "admin/invite-accept.html",
            base_context(
                request,
                token=token,
                invited_user=None,
                error="This invitation link is invalid or has expired. Please contact an admin.",
            ),
        )

    return request.app.state.templates.TemplateResponse(
        "admin/invite-accept.html",
        base_context(
            request,
            token=token,
            invited_user=invited_user,
            error=None,
        ),
    )


@router.post("/invite/accept", response_class=HTMLResponse)
async def admin_invite_accept(
    request: Request,
    token: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Accept an invitation and set password."""
    # Re-fetch invited user for display on error
    invited_user = await get_invite_token_user(db, raw_token=token)

    if password != confirm_password:
        return request.app.state.templates.TemplateResponse(
            "admin/invite-accept.html",
            base_context(
                request,
                token=token,
                invited_user=invited_user,
                error="Passwords do not match.",
            ),
        )

    success, error = await confirm_invitation(db, raw_token=token, password=password)

    if not success:
        return request.app.state.templates.TemplateResponse(
            "admin/invite-accept.html",
            base_context(
                request,
                token=token,
                invited_user=invited_user,
                error=error,
            ),
        )

    return RedirectResponse(url="/admin/invite/success", status_code=303)


@router.get("/invite/success", response_class=HTMLResponse)
async def admin_invite_success(request: Request) -> Response:
    """Display the invitation acceptance success page."""
    return request.app.state.templates.TemplateResponse(
        "admin/invite-success.html",
        base_context(request),
    )
