"""Shared helpers for admin routes."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app.schemas.auth import AuthUser
from app.services.admin_auth_service import (
    ADMIN_SESSION_COOKIE_NAME,
    get_user_for_session_token,
)

# Footer links - shared across all admin pages
FOOTER_LINKS = [
    {"text": "Terms of Service", "url": "/terms"},
    {"text": "Privacy Policy", "url": "/privacy"},
    {"text": "Cookie Policy", "url": "/cookies"},
]


def base_context(request: Request, **extra: Any) -> dict[str, Any]:
    """Build base template context with common values.

    Args:
        request: The FastAPI request object.
        **extra: Additional context values to include.

    Returns:
        Dict with request, footer_links, current_year, and any extra values.
    """
    return {
        "request": request,
        "footer_links": FOOTER_LINKS,
        "current_year": datetime.now().year,
        **extra,
    }


async def get_current_user(
    request: Request,
    db: AsyncSession,
) -> AuthUser | None:
    """Get the current authenticated user from the session cookie.

    Args:
        request: The FastAPI request object.
        db: Database session.

    Returns:
        The authenticated user, or None if not logged in.
    """
    raw_token = request.cookies.get(ADMIN_SESSION_COOKIE_NAME)
    if not raw_token:
        return None
    return await get_user_for_session_token(db, raw_token=raw_token)


async def require_auth(
    request: Request,
    db: AsyncSession,
    next_path: str = "/admin",
) -> tuple[Response | None, AuthUser | None]:
    """Require authentication, redirecting to login if needed.

    Args:
        request: The FastAPI request object.
        db: Database session.
        next_path: Path to redirect back to after login.

    Returns:
        Tuple of (redirect_response, user). If redirect is not None, return it.
    """
    user = await get_current_user(request, db)
    if user is None:
        return (
            RedirectResponse(
                url=f"/admin/login?next={next_path}",
                status_code=303,
            ),
            None,
        )
    return None, user


async def require_admin(
    request: Request,
    db: AsyncSession,
    next_path: str = "/admin/news-sources",
) -> tuple[Response | None, AuthUser | None]:
    """Require admin role, redirecting if not authorized.

    Args:
        request: The FastAPI request object.
        db: Database session.
        next_path: Path to redirect back to after login.

    Returns:
        Tuple of (redirect_response, user). If redirect is not None, return it.
    """
    redirect, user = await require_auth(request, db, next_path)
    if redirect:
        return redirect, None

    if user and user.role != "admin":
        return RedirectResponse(url="/admin", status_code=303), None

    return None, user
