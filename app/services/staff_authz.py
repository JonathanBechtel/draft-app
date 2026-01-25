"""Authorization helpers for staff-only API endpoints."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Literal

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.auth import AuthDatasetPermission, AuthUser
from app.services.admin_auth_service import (
    ADMIN_SESSION_COOKIE_NAME,
    get_user_for_session_token,
)
from app.utils.db_async import get_session

DatasetAction = Literal["view", "edit"]


async def get_current_staff_user(
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> AuthUser:
    """Resolve the current staff user from the session cookie (or raise 401)."""
    raw_token = request.cookies.get(ADMIN_SESSION_COOKIE_NAME)
    if not raw_token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    user = await get_user_for_session_token(db, raw_token=raw_token)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def require_dataset_permission(
    dataset: str, action: DatasetAction
) -> Callable[..., Awaitable[None]]:
    """FastAPI dependency enforcing dataset permissions (raises 401/403)."""

    async def _dependency(
        user: AuthUser = Depends(get_current_staff_user),
        db: AsyncSession = Depends(get_session),
    ) -> None:
        if user.role == "admin":
            return

        if user.role != "worker":
            raise HTTPException(status_code=403, detail="Forbidden")

        async with db.begin():
            result = await db.execute(
                select(AuthDatasetPermission).where(
                    AuthDatasetPermission.user_id == user.id,  # type: ignore[arg-type]
                    AuthDatasetPermission.dataset == dataset,  # type: ignore[arg-type]
                )
            )
            permission = result.scalar_one_or_none()
        if permission is None:
            raise HTTPException(status_code=403, detail="Forbidden")

        if action == "view":
            if not permission.can_view:
                raise HTTPException(status_code=403, detail="Forbidden")
        else:
            if not permission.can_edit:
                raise HTTPException(status_code=403, detail="Forbidden")

    return _dependency
