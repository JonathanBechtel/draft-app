"""Admin panel routes.

This module provides the admin UI routes organized into sub-routers:
- auth: Login, logout, password reset (public routes)
- account: Account view, password change (authenticated routes)
- news_sources: News source CRUD (admin-only routes)
"""

from fastapi import APIRouter

from app.routes.admin.account import router as account_router
from app.routes.admin.auth import router as auth_router
from app.routes.admin.news_sources import router as news_sources_router

router = APIRouter(prefix="/admin", tags=["admin"])

# Include sub-routers
router.include_router(auth_router)
router.include_router(account_router)
router.include_router(news_sources_router)
