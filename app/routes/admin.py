"""Admin UI routes (staff-only auth)."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app.config import settings
from app.services.admin_auth_service import (
    ADMIN_SESSION_COOKIE_NAME,
    REMEMBER_ME_TTL,
    authenticate_staff_user,
    confirm_password_reset,
    enqueue_password_reset,
    get_user_for_session_token,
    issue_session,
    revoke_session,
    sanitize_next_path,
)
from app.utils.db_async import get_session

router = APIRouter(prefix="/admin", tags=["admin"])

# Footer links - shared across all pages
FOOTER_LINKS = [
    {"text": "Terms of Service", "url": "/terms"},
    {"text": "Privacy Policy", "url": "/privacy"},
    {"text": "Cookie Policy", "url": "/cookies"},
]


@router.get("", response_class=HTMLResponse)
async def admin_home(
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Minimal /admin landing page (protected)."""
    raw_token = request.cookies.get(ADMIN_SESSION_COOKIE_NAME)
    if not raw_token:
        return RedirectResponse(
            url="/admin/login?next=/admin",
            status_code=303,
        )

    user = await get_user_for_session_token(db, raw_token=raw_token)
    if user is None:
        return RedirectResponse(
            url="/admin/login?next=/admin",
            status_code=303,
        )

    return request.app.state.templates.TemplateResponse(
        "admin/index.html",
        {
            "request": request,
            "user": user,
            "footer_links": FOOTER_LINKS,
            "current_year": datetime.now().year,
        },
    )


@router.get("/login", response_class=HTMLResponse)
async def admin_login_page(
    request: Request,
    next: str | None = Query(default=None),
) -> Response:
    """Render the login form."""
    return request.app.state.templates.TemplateResponse(
        "admin/login.html",
        {
            "request": request,
            "next": sanitize_next_path(next),
            "error": None,
            "footer_links": FOOTER_LINKS,
            "current_year": datetime.now().year,
        },
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
    if user is None:
        return request.app.state.templates.TemplateResponse(
            "admin/login.html",
            {
                "request": request,
                "next": sanitize_next_path(next),
                "error": "Invalid email or password.",
                "footer_links": FOOTER_LINKS,
                "current_year": datetime.now().year,
            },
            status_code=200,
        )

    remember_me = remember is not None and remember not in {"0", "", "false", "False"}
    if user.id is None:
        return request.app.state.templates.TemplateResponse(
            "admin/login.html",
            {
                "request": request,
                "next": sanitize_next_path(next),
                "error": "Invalid email or password.",
                "footer_links": FOOTER_LINKS,
                "current_year": datetime.now().year,
            },
            status_code=200,
        )
    raw_token, _session = await issue_session(
        db,
        user_id=user.id,
        remember_me=remember_me,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )

    redirect_target = sanitize_next_path(next)
    response = RedirectResponse(url=redirect_target, status_code=303)
    if remember_me:
        response.set_cookie(
            ADMIN_SESSION_COOKIE_NAME,
            raw_token,
            max_age=int(REMEMBER_ME_TTL.total_seconds()),
            httponly=True,
            samesite="lax",
            secure=not settings.is_dev,
            path="/",
        )
    else:
        response.set_cookie(
            ADMIN_SESSION_COOKIE_NAME,
            raw_token,
            httponly=True,
            samesite="lax",
            secure=not settings.is_dev,
            path="/",
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


@router.post("/password-reset", response_class=HTMLResponse)
async def admin_password_reset(
    email: str = Form(...),
    db: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Request a password reset (generic response; inserts outbox email for real users)."""
    await enqueue_password_reset(db, email=email)
    return HTMLResponse(
        "<p>If an account exists, a password reset link has been sent.</p>",
        status_code=200,
    )


@router.post("/password-reset/confirm")
async def admin_password_reset_confirm(
    token: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Consume a reset token and set a new password."""
    ok = await confirm_password_reset(db, raw_token=token, new_password=password)
    if not ok:
        raise HTTPException(status_code=410, detail="Reset token is invalid or expired")

    response = RedirectResponse(url="/admin/login", status_code=303)
    response.delete_cookie(ADMIN_SESSION_COOKIE_NAME, path="/")
    return response
