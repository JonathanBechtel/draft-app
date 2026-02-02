"""Shared helpers for admin routes."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app.schemas.auth import AuthDatasetPermission, AuthUser
from app.services.admin_auth_service import (
    ADMIN_SESSION_COOKIE_NAME,
    get_user_for_session_token,
)
from app.services.admin_permission_service import KNOWN_DATASETS

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


async def base_context_with_permissions(
    request: Request,
    db: AsyncSession,
    user: AuthUser,
    **extra: Any,
) -> dict[str, Any]:
    """Build base template context with common values and user permissions.

    Args:
        request: The FastAPI request object.
        db: Database session.
        user: The authenticated user.
        **extra: Additional context values to include.

    Returns:
        Dict with request, footer_links, current_year, user, permissions, and any extra values.
    """
    permissions = await get_user_permissions_dict(db, user)
    return {
        "request": request,
        "footer_links": FOOTER_LINKS,
        "current_year": datetime.now().year,
        "user": user,
        "permissions": permissions,
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


async def require_dataset_access(
    request: Request,
    db: AsyncSession,
    dataset: str,
    need_edit: bool = False,
    next_path: str = "/admin",
) -> tuple[Response | None, AuthUser | None]:
    """Require access to a specific dataset, redirecting if not authorized.

    Admin users bypass all permission checks.
    Worker users must have explicit permission in AuthDatasetPermission.

    Args:
        request: The FastAPI request object.
        db: Database session.
        dataset: The dataset to check access for (e.g., "players", "images").
        need_edit: If True, require can_edit permission; otherwise can_view is enough.
        next_path: Path to redirect back to after login.

    Returns:
        Tuple of (redirect_response, user). If redirect is not None, return it.
    """
    redirect, user = await require_auth(request, db, next_path)
    if redirect:
        return redirect, None

    if user is None:
        return RedirectResponse(
            url=f"/admin/login?next={next_path}", status_code=303
        ), None

    # Admins have full access to everything
    if user.role == "admin":
        return None, user

    # Workers need explicit permission
    if user.id is None:
        return RedirectResponse(url="/admin", status_code=303), None

    result = await db.execute(
        select(AuthDatasetPermission).where(
            AuthDatasetPermission.user_id == user.id,  # type: ignore[arg-type]
            AuthDatasetPermission.dataset == dataset,  # type: ignore[arg-type]
        )
    )
    permission = result.scalar_one_or_none()

    if permission is None:
        return RedirectResponse(url="/admin", status_code=303), None

    # Check appropriate permission based on operation type
    # can_edit implies access for edit routes; can_view required for view-only routes
    if need_edit:
        if not permission.can_edit:
            return RedirectResponse(url="/admin", status_code=303), None
    else:
        if not permission.can_view:
            return RedirectResponse(url="/admin", status_code=303), None

    return None, user


async def get_user_permissions_dict(
    db: AsyncSession,
    user: AuthUser,
) -> dict[str, dict[str, bool]]:
    """Get permissions for a user as a dict for template use.

    Args:
        db: Database session.
        user: The authenticated user.

    Returns:
        Dict mapping dataset names to {"can_view": bool, "can_edit": bool}.
        Admins get all permissions set to True.
    """
    # Admins have full access
    if user.role == "admin":
        return {
            dataset: {"can_view": True, "can_edit": True} for dataset in KNOWN_DATASETS
        }

    # Workers need explicit permissions
    if user.id is None:
        return {
            dataset: {"can_view": False, "can_edit": False}
            for dataset in KNOWN_DATASETS
        }

    result = await db.execute(
        select(AuthDatasetPermission).where(
            AuthDatasetPermission.user_id == user.id  # type: ignore[arg-type]
        )
    )
    existing = {p.dataset: p for p in result.scalars().all()}

    permissions: dict[str, dict[str, bool]] = {}
    for dataset in KNOWN_DATASETS:
        if dataset in existing:
            perm = existing[dataset]
            permissions[dataset] = {
                "can_view": perm.can_view,
                "can_edit": perm.can_edit,
            }
        else:
            permissions[dataset] = {
                "can_view": False,
                "can_edit": False,
            }

    return permissions
