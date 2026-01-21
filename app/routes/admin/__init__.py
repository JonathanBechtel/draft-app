"""Admin panel routes.

This module provides the admin UI routes organized into sub-routers:
- auth: Login, logout, password reset (public routes)
- account: Account view, password change (authenticated routes)
- news_sources: News source CRUD (admin-only routes)
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app.routes.admin.account import router as account_router
from app.routes.admin.auth import router as auth_router
from app.routes.admin.helpers import base_context, get_current_user
from app.routes.admin.news_sources import router as news_sources_router
from app.utils.db_async import get_session

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("", response_class=HTMLResponse)
async def admin_home(
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Admin dashboard landing page (protected)."""
    user = await get_current_user(request, db)
    if user is None:
        return RedirectResponse(url="/admin/login?next=/admin", status_code=303)

    return request.app.state.templates.TemplateResponse(
        "admin/index.html",
        base_context(request, user=user),
    )


# Include sub-routers
router.include_router(auth_router)
router.include_router(account_router)
router.include_router(news_sources_router)
